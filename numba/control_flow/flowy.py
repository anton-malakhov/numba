# -*- coding: utf-8 -*-

"""
Flow graph and operation for programs.
"""

from __future__ import print_function, division, absolute_import

import collections

from llvmpy.api import llvm

from numba.control_flow import basicblocks
from numba.experimental import llvm_passes, llvm_types, llvm_utils, llvm_const
from numba.experimental.llvm_utils import llvm_context

def make_temper():
    temps = collections.defaultdict(int)

    def temper(name):
        count = temps[name]
        temps[name] += 1
        if name and count == 0:
            return name
        elif name:
            return '%s_%d' % (name, count)
        else:
            return str(count)

    return temper


class FunctionGraph(object):

    def __init__(self, name, blocks=None, temper=None):
        self.name = name

        # Basic blocks in topological order
        self.blocks = blocks or []

        # name -> temp_name
        self.temper = temper or make_temper()

    def __repr__(self):
        return "FunctionGraph(%s)" % self.blocks

class Block(basicblocks.BasicBlock):

    def __init__(self, id, label, pos):
        super(Block, self).__init__(id, label, pos)

        self.funcgraph = None   # FunctionGraph that owns us

        # [Variable]
        self.instrs = []

    def __iter__(self):
        return iter(self.instrs)

    def append(self, instr):
        self.instrs.append(instr)

    def extend(self, instrs):
        self.instrs.extend(instrs)

    def __repr__(self):
        return "Block(%s, %s, %s)" % (self.id, self.label, self.instrs)

# ______________________________________________________________________

class OperationBuilder(object):

    def __init__(self, funcgraph, Block=Block):
        self.funcgraph = funcgraph

        self.Block = Block
        self.Variable = Variable
        self.Operation = Operation
        self.Constant = Constant

        self.block_temper = make_temper()

    def add_block(self, parents, name=None, pos=None):
        name = self.block_temper(name or "block")
        block = self.Block(len(self.funcgraph.blocks), name, pos)
        block.funcgraph = self.funcgraph

        self.funcgraph.blocks.append(block)

        for parent in parents:
            block.add_parents(parent)

        return block

    def create_variable(self, operation, name=""):
        var = self.Variable(self.block_temper(name), operation)
        operation.result = var

        # Update uses list
        for arg in operation.args:
            if arg.is_var:
                arg.uses.append(var)

        return var

    def op(self, opcode, args, name=""):
        "Add an operation to the graph, unlinked from any basic block"
        return self.create_variable(self.Operation(opcode, args), name)

    def const(self, pyval):
        return self.Constant(pyval)

    #------------------------------------------------------------------------
    # Some utilities for dealing with basic blocks
    #------------------------------------------------------------------------

    def splitblock(self, block, instr, name=None):
        children = block.children
        block.detach_children()
        newblock = self.add_block([block], name)
        newblock.add_children(*children)

        split_idx = block.instrs.index(instr) # TODO: allow for linked lists
        before, after = block.instrs[:split_idx], block.instrs[split_idx:]
        block.instrs = before
        newblock.instrs = after

        return newblock

    def if_then_else(self, block, instr, condition, if_body, else_body=None):
        cond_block = self.add_block([block])
        cond_block.append(condition)

        exit_block = self.splitblock(block, instr)
        exit_block.detach_parents()

        if_block = self.add_block([cond_block])
        if_block.instrs = if_body

        if else_body:
            else_block = self.add_block([cond_block])
            else_block.instrs = else_body
        else:
            else_block = cond_block

        exit_block.add_parents([if_block, else_block])
        # return if_block, else_block, exit_block


class Opcode(object):
    """
    Opcode used to indicate the type of Operation.

        op: an opcode representation, e.g. the string 'call'
        sideeffects: do operations of this opcode have sideeffects?
        read: do operations of this opcode read memory?
        write: do operations of this opcode write memory?
        canfold: can we constant fold operations of this opcode?
                 (if they have constant arguments)
        exceptions: set of exceptions the operation may raise
    """

    def __init__(self, concrete_opcode, sideeffects=True, canfold=False,
                 read=True, exceptions=()):
        self.op = concrete_opcode

        self.sideeffects = sideeffects #or write
        self.read = read
        # self.write = write
        self.canfold = canfold
        self.exceptions = exceptions

    def __repr__(self):
        return repr(self.op)

    def __str__(self):
        return str(self.op)

class Operation(object):
    """
    We can employ two models:

        1) operation(opcode, args, result)

            Each operation has a result/target/variable.
            We can retrieve the operations you refer to through
            a variable store: { Variable : Operation } (i.e. use -> def)

            However, this store needs to be constructed each pass in a
            forward manner, or the store needs to be kept up to date.
            We can write transformations like:

                ops_x = {}
                for block in blocks:
                    for i, op in enumerate(block.ops):
                        if op == 'X':
                            ops_x[op.result] = op
                        elif op == 'Y' and op.args[0] in ops_x:
                            x_arg = ops_x[op.args[0]]
                            block.ops[i] = Operation('Z', op.args, op.result)

        2) operation(opcode, args)

            Each operation is either a result/target/variable (LLVM) or
            a Value has an operation.

                class Value:
                    Use *UseList
                    Type type

                class User(Value):
                    Use *OperandList

            Example:

                %0 = X()            # oplist=[] uselist=[%2]
                %1 = Y()            # oplist=[] uselist=[%2]
                %2 = Z(%0, %1)      # oplist=[%0, %1] uselist=[]

            At any point we can efficiently match a pattern Z(X(), *):
    """

    def __init__(self, opcode, args):
        self.opcode = opcode
        # [Variable | Constant]
        self.args = args

        # This is set by Builder.create_operation
        self.result = None

    def __repr__(self):
        args = [var.varname if var.is_var else var for var in self.args]
        return "Operation(%s, %s)" % (self.opcode, args)

class Value(object):
    """
    The result of an Operation.
    """

    is_var = False
    is_const = False


class Variable(Value):

    is_var = True

    def __init__(self, name, operation, type=None):
        self.name = str(name)
        self.operation = operation
        self.type = type
        self.uses = []

    def replace(self, other):
        self.operation = other

    @property
    def varname(self):
        return '%' + self.name

    @property
    def opcode(self):
        """
        Opcode of the operation of this variable.
        """
        return self.operation.opcode

    @property
    def opname(self):
        """
        Opcode name or object associated with the operation of this variable.
        """
        return self.opcode.op

    def __repr__(self):
        return "%s = %s" % (self.varname, self.operation)


class Constant(Value):
    "Constant value. Immutable!"

    is_const = True

    def __init__(self, const, type=None):
        self._const = const
        self.type = type

    @property
    def const(self):
        return self._const

    def __repr__(self):
        return "const(%s)" % self.const

# ______________________________________________________________________

class FunctionGraphContext(object):

    def __init__(self, opctx, constfolder):
        self.opctx = opctx
        self.constfolder = constfolder

    def return_value(self, funcgraph):
        """
        :return: The return value of this function represented by `funcgraph`
        """
        return None

    # ....
    # delegations here

class OperationContext(object):

    def is_pure(self, operation):
        op = operation.opcode
        return not op.sideeffects and op.canfold

    def is_terminator(self, operation):
        raise NotImplementedError("is_terminator")

    def is_return(self, operation):
        raise NotImplementedError("is_return")

    def is_conditional_branch(self, operation):
        raise NotImplementedError("is_conditional_branch")

    def get_condition(self, conditional_branch):
        return conditional_branch.args[0]

    def is_boolean_operation(self, operation):
        raise NotImplementedError

    def opname(self, opcode):
        return str(opcode.op)


class ConstantFolder(object):

    def fold(self, operation):
        """
        Try to fold the operation if all arguments are Constant.

        :return: Constant or Variable

            In case the operation cannot be folded, simply return `operation`
        """
        return operation

# ______________________________________________________________________
# LLVM stuff

llvm_value_t = llvm.StructType.create(llvm_context, "unknown")
unknown_ptr = llvm.PointerType.getUnqual(llvm_value_t)

class LLVMBuilder(object):

    def __init__(self, name, opague_type, argnames):
        self.name = name
        self.opague_type = opague_type
        self.lmod = self.make_module(name)
        self.lfunc = self.make_func(self.lmod, name, opague_type, argnames)
        self.builder = self.make_builder(self.lfunc)

    # __________________________________________________________________

    @classmethod
    def make_module(cls, name):
        mod = llvm.Module.new('module.%s' % name, llvm_context)
        return mod

    @classmethod
    def make_func(cls, lmod, name, opague_type, argnames):
        argtys = [opague_type] * len(argnames)
        restype = opague_type
        lfunc = llvm_utils.get_or_insert_func(
            lmod, name, restype, argtys)
        return lfunc

    @classmethod
    def make_builder(cls, lfunc):
        # entry = cls.make_block(lfunc, "entry")
        builder = llvm.IRBuilder.new(llvm_context)
        # builder.SetInsertPoint(entry)
        return builder

    @classmethod
    def make_block(cls, lfunc, blockname):
        blockname = 'block_%s' % blockname
        return llvm_utils.make_basic_block(lfunc, blockname)

    # __________________________________________________________________

    def delete(self):
        self.lmod = None
        self.lfunc = None
        self.builder = None

    def verify(self):
        llvm_utils.verify_module(self.lmod)

    def add_block(self, blockname):
        return self.make_block(self.lfunc, blockname)

    def set_block(self, dstblock):
        self.builder.SetInsertPoint(dstblock)

    def run_passes(self, passes):
        llvm_passes.run_function_passses(self.lfunc, passes)

    def call_abstract(self, name, restype, *args):
        argtys = [x.getType() for x in args]
        callee = llvm_utils.get_or_insert_func(self.lmod, name,
                                               restype, argtys)
        callee.setLinkage(llvm.GlobalValue.LinkageTypes.ExternalLinkage)
        return self.builder.CreateCall(callee, args)

    def call_abstract_pred(self, name, *args):
        argtys = [x.getType() for x in args]
        retty = llvm_types.i1
        callee = llvm_utils.get_or_insert_func(self.lmod, name,
                                               retty, argtys)
        return self.builder.CreateCall(callee, args)


class LLVMOperationTyper(object):

    def __init__(self, builder):
        self.builder = builder

    def llvm_restype(self, operation):
        return self.builder.opague_type


class LLVMMapper(object):

    LLVMBuilder = LLVMBuilder

    def __init__(self, funcgraph, opctx):
        self.funcgraph = funcgraph
        self.opctx = opctx
        self.builder = self.LLVMBuilder(funcgraph.name,
                                        llvm_types.unknown_ptr, [])

        # Operation -> LLVM Value
        self.llvm_values = {}

        # Block -> llvm block
        self.llvm_blocks = {}

    def llvm_operation(self, operation, llvm_args):
        name = self.opctx.opname(operation.opcode)
        # include operation arity in name
        name = '%s_%d' % (name, len(operation.args))
        if self.opctx.is_boolean_operation(operation):
            restype = llvm_types.i1
        else:
            restype = self.builder.opague_type
        return self.builder.call_abstract(name, restype, *llvm_args)

    def process_op(self, var):
        # TODO: map this properly
        args = [self.llvm_values[arg]
                    for arg in var.operation.args if arg.is_var]
        value = self.llvm_operation(var.operation, args)
        self.llvm_values[var] = value

    def make_llvm_graph(self):
        "Populate the LLVM Function with abstract IR"
        blocks = self.funcgraph.blocks
        assert blocks

        # Allocate blocks
        for block in blocks:
            self.llvm_blocks[block] = self.builder.add_block(block.label)

        # Generete abstract IR
        for block in blocks:
            # print("block", block.label, len(block.children))
            self.builder.builder.SetInsertPoint(self.llvm_blocks[block])

            for var in block.instrs[:-1]:
                self.process_op(var)

            if len(block.children) == 1:
                if block.instrs: self.process_op(block.instrs[-1])
                succ, = block.children
                self.builder.builder.CreateBr(self.llvm_blocks[succ])
            elif block.instrs:
                self.terminate_block(block)

        if not block.instrs or not self.opctx.is_return(blocks[-1]):
            # Terminate with return
            #self.builder.builder.CreateRetVoid()
            self.builder.builder.CreateRet(
                llvm_const.null(self.builder.opague_type))

        print(self.builder.lfunc)
        self.builder.verify()
        return self.builder.lfunc

    def terminate_block(self, block):
        "Terminate a block with conditional branch"
        op = block.instrs[-1].operation

        assert self.opctx.is_terminator(op), op
        assert self.opctx.is_conditional_branch(op)
        assert len(block.children) == 2

        cond = self.opctx.get_condition(op)
        lcond = self.llvm_values[cond]

        succ1, succ2 = block.children
        self.builder.builder.CreateCondBr(
            lcond, self.llvm_blocks[succ1], self.llvm_blocks[succ2])
