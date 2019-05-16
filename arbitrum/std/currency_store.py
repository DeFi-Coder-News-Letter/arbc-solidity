from ..annotation import modifies_stack
from . import keyvalue_int_int
from .stack_manip import dup_n
from .struct import Struct
from .. import value

# currencyStore keep track of balances of currencies
# implemented as a keyvalue store, currencyid to balance

currency_store = Struct("currency_store", [
    ("store", keyvalue_int_int.typ)
])

typ = currency_store.typ

@modifies_stack(0, [typ])
def new(vm):
    keyvalue_int_int.new(vm)


@modifies_stack([typ, value.IntType()], [value.IntType()])
def get(vm):
    # cstore currId -> balance
    currency_store.get("store")(vm)
    keyvalue_int_int.get(vm)
    # value


@modifies_stack(
    [typ, value.IntType(), value.IntType()],
    [typ]
)
def add(vm):
    # cstore currId delta -> updatedcstore
    vm.dup1()
    vm.dup1()
    get(vm)
    # oldval cstore currId delta
    dup_n(3)(vm)
    vm.add()
    # newval cstore currId delta
    vm.swap2()
    vm.swap1()
    # cstore currId newval delta
    currency_store.get("store")(vm)
    keyvalue_int_int.set_val(vm)
    currency_store.set_val("store")(vm)
    # updatedcstore delta
    vm.swap1()
    vm.pop()


@modifies_stack([
    typ,
    value.IntType(),
    value.IntType()
], [
    value.IntType(),
    typ
])
def deduct(vm):
    # cstore currId delta -> success updatedcstore
    vm.dup1()
    vm.dup1()
    get(vm)
    # oldval cstore currId delta
    dup_n(3)(vm)
    # delta oldval cstore currId delta
    vm.dup1()
    vm.dup1()
    vm.gt()
    vm.iszero()
    vm.ifelse(lambda vm: [
        vm.add(),
        # newval cstore currId delta
        vm.swap2(),
        vm.swap1(),
        # cstore currId newval delta
        currency_store.get("store")(vm),
        keyvalue_int_int.set_val(vm),
        currency_store.set_val("store")(vm),
        # updatedcstore delta
        vm.swap1(),
        vm.pop(),
        vm.push(1),
    ], lambda vm: [
        vm.pop(),
        vm.pop(),
        vm.swap2(),
        vm.pop(),
        vm.pop(),
        vm.push(0)
    ])