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

import json
import arbitrum as arb
import eth_utils
from collections import Counter
from arbitrum.instructions import OPS
import sys
from arbitrum.evm.contract import ArbContract, create_evm_vm

from arbitrum.value import *
from arbitrum.std import sized_byterange

def run_until_halt(vm):
    log = []
    i = 0
    push_counts = Counter()
    while True:
        try:
            if vm.pc.op.get_op() == OPS["spush"]:
                push_counts[vm.pc.path[-1][5:-1]] += 1
            run = arb.run_vm_once(vm)
            if not run:
                print("Hit blocked insn")
                break
            i += 1
        except Exception as err:
            print("Error at", vm.pc.pc - 1, vm.code[vm.pc.pc - 1])
            print("Context", vm.code[vm.pc.pc - 6: vm.pc.pc + 4])
            raise err
        if vm.halted:
            break
    for log in vm.logs:
        vm.output_handler(log)
    vm.logs = []
    print("Ran VM for {} steps".format(i))
    # print(push_counts)
    return log


def run_n_steps(vm, steps):
    log = []
    i = 0
    while i < steps:
        log.append((vm.pc.pc, vm.stack[:]))
        try:
            # print(vm.pc, vm.stack[:])
            arb.run_vm_once(vm)
            i += 1
        except Exception as err:
            print("Error at", vm.pc.pc - 1, vm.code[vm.pc.pc - 1])
            print("Context", vm.code[vm.pc.pc - 6: vm.pc.pc + 4])
            raise err
        if vm.halted:
            break
    print("Ran VM for {} steps".format(i))
    return log

def make_msg_val(calldata):
    return arb.value.Tuple([calldata, 0, 0, 0])

if __name__ == '__main__':
    # tup = Tuple([Tuple([
    #     Tuple(),
    #     Tuple([Tuple(), Tuple(), Tuple(), Tuple(), Tuple(), Tuple(), Tuple(), 0]),
    #     Tuple(),
    #     Tuple(),
    #     Tuple(),
    #     Tuple(),
    #     Tuple(),
    #     1
    # ]), 64])
    # print(tup)
    # print(sized_byterange.tohex(tup))
    # print(int("0xada5013122d395ba3c54772283fb069b10426056ef8ca54750cb9bb552a59e7d", 0))
    data = bytearray.fromhex("00000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000000")
    intHash = int.from_bytes(eth_utils.crypto.keccak(data), byteorder="big")
    print(intHash)
    print(hex(intHash))
    # sys.exit(0)
    if len(sys.argv) != 2:
        raise Exception("Call as truffle_runner.py [compiled.json]")

    with open(sys.argv[1]) as json_file:
        raw_contracts = json.load(json_file)

    contracts = [ArbContract(contract) for contract in raw_contracts]
    vm = create_evm_vm(contracts)
    with open("code.txt", "w") as f:
        for instr in vm.code:
            f.write("{} {}".format(instr, instr.path))
            f.write("\n")

    elections = contracts[0]

    print(elections._candidatesCount())
    print(elections._candidates(1))
    print(elections._candidates(2))

    # vm.env.send_message([elections.candidatesCount(), 2345, 0, 0, 0])
    vm.env.send_message([make_msg_val(elections.candidates(14, 1)), 2345, 0, 0, 0])
    vm.env.send_message([make_msg_val(elections.vote(14, 1)), 2345, 0, 0, 0])
    vm.env.send_message([make_msg_val(elections.candidates(14, 1)), 2345, 0, 0, 0])
    # vm.env.send_message([elections.candidates(2), 2345, 0, 0, 0])
    vm.env.deliver_pending()
    run_until_halt(vm)

