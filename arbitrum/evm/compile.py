# Copyright 2019, Offchain Labs, Inc.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pyevmasm

from ..annotation import modifies_stack
from ..std import stack_manip
from ..std import byterange, bitwise
from ..vm import AVMOp
from ..ast import AVMLabel
from ..ast import BlockStatement
from ..compiler import compile_block
from .. import value

from . import os, call_frame
from . import execution

import eth_utils


class EVMNotSupported(Exception):
    """VM tried to run opcode that blocks"""
    pass


def replace_self_balance(instrs):
    out = []
    i = 0
    while i < len(instrs):
        if (
                instrs[i].name == "ADDRESS"
                and instrs[i + 1].name == "PUSH20"
                and instrs[i + 1].operand == (2**160 - 1)
                and instrs[i + 2].name == "AND"
                and instrs[i + 3].name == "BALANCE"
        ):
            out.append(AVMOp("SELF_BALANCE"))
            i += 4
        else:
            out.append(instrs[i])
            i += 1
    return out


def contains_endcode(val):
    if not isinstance(val, int):
        return False
    while val >= 0xa265:
        if (val&0xffff == 0xa165) or (val&0xffff == 0xa265):
            return True
        val = val//256
    return False


def remove_metadata(instrs):
    for i in range(len(instrs) - 2, -1, -1):
        first_byte = instrs[i].opcode
        second_byte = instrs[i + 1].opcode
        if ((first_byte == 0xa1) or (first_byte == 0xa2)) and second_byte == 0x65:
            return instrs[:i]
        if hasattr(instrs[i], 'operand') and contains_endcode(instrs[i].operand):
            # leave the boundary instruction in--it's probably valid and minimal testing suggests this reduces errors
            return instrs[:i+1]
    return instrs


def make_bst_lookup(items):
    return _make_bst_lookup([(x, items[x]) for x in sorted(items)])


def _make_bst_lookup(items):
    def handle_bad_jump(vm):
        vm.tnewn(0)

    if len(items) >= 3:
        mid = len(items) // 2
        left = items[:mid]
        right = items[mid + 1:]
        pivot = items[mid][0]

        def impl_n(vm):
            # index
            vm.push(pivot)
            vm.dup1()
            vm.lt()
            # index < pivot, index
            vm.ifelse(lambda vm: [
                # index < pivot
                _make_bst_lookup(left)(vm)
            ], lambda vm: [
                vm.push(pivot),
                vm.dup1(),
                vm.gt(),
                vm.ifelse(lambda vm: [
                    # index > pivot
                    _make_bst_lookup(right)(vm)
                ], lambda vm: [
                    # index == pivot
                    vm.pop(),
                    vm.push(items[mid][1])
                ])
            ])
        return impl_n

    if len(items) == 2:
        def impl_2(vm):
            # index
            vm.dup0()
            vm.push(items[0][0])
            vm.eq()
            vm.ifelse(lambda vm: [
                vm.pop(),
                vm.push(items[0][1])
            ], lambda vm: [
                vm.push(items[1][0]),
                vm.eq(),
                vm.ifelse(lambda vm: [
                    vm.push(items[1][1])
                ], handle_bad_jump)
            ])
        return impl_2

    if len(items) == 1:
        def impl_1(vm):
            vm.push(items[0][0])
            vm.eq()
            vm.ifelse(lambda vm: [
                vm.push(items[0][1])
            ], handle_bad_jump)
        return impl_1

    return handle_bad_jump


def generate_evm_code(raw_code, storage):
    contracts = {}
    for contract in raw_code:
        contracts[contract] = list(pyevmasm.disassemble_all(
            raw_code[contract]
        ))

    code_tuples_data = {}
    for contract in raw_code:
        code_tuples_data[contract] = byterange.frombytes(
            bytes.fromhex(raw_code[contract].hex())
        )
    code_tuples_func = make_bst_lookup(code_tuples_data)

    @modifies_stack([value.IntType()], 1)
    def code_tuples(vm):
        code_tuples_func(vm)

    code_hashes_data = {}
    for contract in raw_code:
        code_hashes_data[contract] = int.from_bytes(
            eth_utils.crypto.keccak(raw_code[contract]),
            byteorder="big"
        )
    code_hashes_func = make_bst_lookup(code_hashes_data)

    @modifies_stack([value.IntType()], [value.ValueType()])
    def code_hashes(vm):
        code_hashes_func(vm)

    contract_dispatch = {}
    for contract in contracts:
        contract_dispatch[contract] = AVMLabel("contract_entry_" + str(contract))
    contract_dispatch_func = make_bst_lookup(contract_dispatch)

    @modifies_stack([value.IntType()], 1)
    def dispatch_contract(vm):
        contract_dispatch_func(vm)

    code_sizes = {}
    for contract in contracts:
        code_sizes[contract] = len(contracts[contract]) + sum(op.operand_size for op in contracts[contract])

    # Give the interrupt contract address a nonzero size
    code_sizes[0x01] = 1
    code_size_func = make_bst_lookup(code_sizes)

    @modifies_stack([value.IntType()], 1)
    def code_size(vm):
        code_size_func(vm)

    impls = []
    contract_info = []
    for contract in sorted(contracts):
        if contract not in storage:
            storage[contract] = {}
        impls.append(generate_contract_code(
            AVMLabel("contract_entry_" + str(contract)),
            contracts[contract],
            code_tuples_data[contract],
            contract,
            code_size,
            code_tuples,
            code_hashes,
            dispatch_contract
        ))
        contract_info.append({
            "contractID": contract,
            "storage": storage[contract]
        })

    def initialization(vm):
        os.initialize(vm, contract_info)
        vm.jump_direct(AVMLabel("run_loop_start"))

    def run_loop_start(vm):
        vm.set_label(AVMLabel("run_loop_start"))
        os.get_next_message(vm)
        execution.setup_initial_call(vm, dispatch_contract)
        vm.push(AVMLabel("run_loop_start"))
        vm.jump()

    main_code = []
    main_code.append(compile_block(run_loop_start))
    main_code += impls
    return compile_block(initialization), BlockStatement(main_code)


def generate_contract_code(label, code, code_tuple, contract_id, code_size, code_tuples, code_hashes, dispatch_contract):
    code = remove_metadata(code)
    code = replace_self_balance(code)

    jump_table = {}
    for insn in code:
        if insn.name == "JUMPDEST":
            jump_table[insn.pc] = AVMLabel("jumpdest_{}_{}".format(contract_id, insn.pc))
    dispatch_func = make_bst_lookup(jump_table)

    @modifies_stack([value.IntType()], [value.ValueType()], contract_id)
    def dispatch(vm):
        dispatch_func(vm)

    @modifies_stack(0, 1, contract_id)
    def get_contract_code(vm):
        vm.push(code_tuple)

    def run_op(instr):
        def impl(vm):
            if instr.name == "SELF_BALANCE":
                vm.push(0)
                os.balance_get(vm)

            # 0s: Stop and Arithmetic Operations
            elif instr.name == "STOP":
                execution.stop(vm)
            elif instr.name == "ADD":
                vm.add()
            elif instr.name == "MUL":
                vm.mul()
            elif instr.name == "SUB":
                vm.sub()
            elif instr.name == "DIV":
                vm.dup1()
                vm.iszero()
                vm.ifelse(
                    lambda vm: vm.pop(),
                    lambda vm: vm.div()
                )
            elif instr.name == "SDIV":
                vm.dup1()
                vm.iszero()
                vm.ifelse(
                    lambda vm: vm.pop(),
                    lambda vm: vm.sdiv()
                )
            elif instr.name == "MOD":
                vm.dup1()
                vm.iszero()
                vm.ifelse(
                    lambda vm: vm.pop(),
                    lambda vm: vm.mod()
                )
            elif instr.name == "SMOD":
                vm.dup1()
                vm.iszero()
                vm.ifelse(
                    lambda vm: vm.pop(),
                    lambda vm: vm.smod()
                )
            elif instr.name == "ADDMOD":
                vm.dup2()
                vm.iszero()
                vm.ifelse(
                    lambda vm: [vm.pop(), vm.pop()],
                    lambda vm: vm.addmod()
                )
            elif instr.name == "MULMOD":
                vm.dup2()
                vm.iszero()
                vm.ifelse(
                    lambda vm: [vm.pop(), vm.pop()],
                    lambda vm: vm.mulmod()
                )
            elif instr.name == "EXP":
                vm.exp()
            elif instr.name == "SIGNEXTEND":
                vm.signextend()

            # 10s: Comparison & Bitwise Logic Operations
            elif instr.name == "LT":
                vm.lt()
            elif instr.name == "GT":
                vm.gt()
            elif instr.name == "SLT":
                vm.slt()
            elif instr.name == "SGT":
                vm.sgt()
            elif instr.name == "EQ":
                vm.eq()
            elif instr.name == "ISZERO":
                vm.iszero()
            elif instr.name == "AND":
                vm.bitwise_and()
            elif instr.name == "OR":
                vm.bitwise_or()
            elif instr.name == "XOR":
                vm.bitwise_xor()
            elif instr.name == "NOT":
                vm.bitwise_not()
            elif instr.name == "BYTE":
                vm.byte()
            elif instr.opcode == 0x1b:
                vm.swap1()
                bitwise.shift_left(vm)
            elif instr.opcode == 0x1c:
                vm.swap1()
                bitwise.shift_right(vm)
            elif instr.opcode == 0x1d:
                vm.swap1()
                bitwise.arithmetic_shift_right(vm)

            # 20s: SHA3
            elif instr.name == "SHA3":
                os.evm_sha3(vm)

            # 30s: Environmental Information
            elif instr.name == "ADDRESS":
                os.get_call_frame(vm)
                call_frame.call_frame.get("contractID")(vm)
            elif instr.name == "ORIGIN":
                os.message_origin(vm)
            elif instr.name == "CALLER":
                os.message_caller(vm)
            elif instr.name == "CALLVALUE":
                os.message_value(vm)
            elif instr.name == "CALLDATALOAD":
                os.message_data_load(vm)
            elif instr.name == "CALLDATASIZE":
                os.message_data_size(vm)
            elif instr.name == "CALLDATACOPY":
                os.message_data_copy(vm)
            elif instr.name == "CODESIZE":
                vm.push(len(code))
            elif instr.name == "CODECOPY":
                os.evm_copy_to_memory(vm, get_contract_code)
            elif instr.name == "GASPRICE":
                # TODO: Arbitrary value
                vm.push(1)
            elif instr.name == "RETURNDATASIZE":
                os.return_data_size(vm)
            elif instr.name == "RETURNDATACOPY":
                os.return_data_copy(vm)

            # 40s: Block Information
            elif instr.name == "BLOCKHASH":
                raise EVMNotSupported(instr.name)
            elif instr.name == "COINBASE":
                raise EVMNotSupported()
            elif instr.name == "TIMESTAMP":
                os.get_timestamp(vm)
            elif instr.name == "NUMBER":
                os.get_block_number(vm)
            elif instr.name == "DIFFICULTY":
                raise EVMNotSupported(instr.name)
            elif instr.name == "GASLIMIT":
                # TODO: Arbitrary value
                raise EVMNotSupported(instr.name)

            # 50s: Stack, Memory, Storage and Flow Operations
            elif instr.name == "POP":
                vm.pop()
            elif instr.name == "MLOAD":
                os.memory_load(vm)
            elif instr.name == "MSTORE":
                os.memory_store(vm)
            elif instr.name == "MSTORE8":
                os.memory_store8(vm)
            elif instr.name == "SLOAD":
                os.storage_load(vm)
            elif instr.name == "SSTORE":
                os.storage_store(vm)
            elif instr.name == "JUMP":
                dispatch(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.error()
                ], lambda vm: [
                    vm.jump()
                ])
            elif instr.name == "JUMPI":
                dispatch(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.error()
                ], lambda vm: [
                    vm.cjump()
                ])
            elif instr.name == "GETPC":
                raise EVMNotSupported(instr.name)
            elif instr.name == "MSIZE":
                os.memory_length(vm)
            elif instr.name == "GAS":
                vm.push(9999999999)
                # TODO: Fill in here
            elif instr.name == "JUMPDEST":
                vm.set_label(AVMLabel("jumpdest_{}_{}".format(contract_id, instr.pc)))

            # 60s & 70s: Push Operations
            elif instr.name[:4] == "PUSH":
                vm.push(instr.operand)

            # 80s: Duplication Operations
            elif instr.name[:3] == "DUP":
                dup_num = int(instr.name[3:]) - 1
                if dup_num == 0:
                    vm.dup0()
                elif dup_num == 1:
                    vm.dup1()
                elif dup_num == 2:
                    vm.dup2()
                else:
                    stack_manip.dup_n(dup_num)(vm)

            # 90s: Exchange Operations
            elif instr.name[:4] == "SWAP":
                swap_num = int(instr.name[4:])
                if swap_num == 1:
                    vm.swap1()
                elif swap_num == 2:
                    vm.swap2()
                else:
                    stack_manip.swap_n(swap_num)(vm)

            # a0s: Logging Operations
            elif instr.name == "LOG1":
                os.evm_log1(vm)
            elif instr.name == "LOG2":
                os.evm_log2(vm)
            elif instr.name == "LOG3":
                os.evm_log3(vm)
            elif instr.name == "LOG4":
                os.evm_log4(vm)

            # f0s: System operations
            elif instr.name == "CREATE":
                raise EVMNotSupported(instr.name)
            elif instr.opcode == 0xf5:
                raise EVMNotSupported("CREATE2")
            elif instr.name == "CALL":
                execution.call(vm, dispatch_contract, instr.pc, contract_id)
            elif instr.name == "CALLCODE":
                execution.callcode(vm, dispatch_contract, instr.pc, contract_id)
            elif instr.name == "RETURN":
                execution.ret(vm)
            elif instr.name == "DELEGATECALL":
                execution.delegatecall(vm, dispatch_contract, instr.pc, contract_id)
            elif instr.name == "STATICCALL":
                execution.staticcall(vm, dispatch_contract, instr.pc, contract_id)
            elif instr.name == "REVERT":
                execution.revert(vm)
            elif instr.name == "SELFDESTRUCT":
                execution.selfdestruct(vm)
            elif instr.name == "BALANCE":
                print("Warning: BALANCE was used which may lead to an error")
                vm.push(0)
                vm.swap1()
                os.ext_balance(vm)
            elif instr.name == "EXTCODESIZE":
                print("Warning: EXTCODESIZE was used which may lead to an error")
                code_size(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.error()
                ])
            elif instr.name == "EXTCODECOPY":
                print("Warning: EXTCODECOPY was used which may lead to an error")
                code_tuples(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.error()
                ])
                os.set_scratch(vm)
                os.evm_copy_to_memory(vm, os.get_scratch)
            elif instr.opcode == 0x3f:  # EXTCODEHASH
                print("Warning: EXTCODEHASH was used which may lead to an error")
                code_hashes(vm)
                vm.dup0()
                vm.tnewn(0)
                vm.eq()
                vm.ifelse(lambda vm: [
                    vm.error()
                ])
            elif instr.name == "INVALID":
                if instr.opcode != 0xfe:
                    print("Warning: Source code contained nonstandard invalid opcode {}".format(hex(instr.opcode)))
                execution.revert(vm)
            else:
                raise Exception("Unhandled instruction {}".format(instr))
        return impl

    contract_code = [label]
    for insn in code:
        block = compile_block(run_op(insn))
        block.add_node("EthOp({}, {})".format(insn, insn.pc))
        contract_code.append(block)

    return BlockStatement(contract_code)
