"""Pet capture rejects incomplete snapshots, including wrong-type empty save lists."""
import struct

import pytest

from config.offsets import Array, Class, List, PetSaveData, PlayerSaveData
from game.build import read_pets

PSD, PETS, ITEMS = 0x1000, 0x2000, 0x3000
ARRAY_CLASS, PET_CLASS, NAME = 0x10000, 0x20000, 0x30000
PET1, PET2 = 0x4000, 0x5000


class PetReader:
    def __init__(self, rows=((101, True), (102, False))):
        self.mem = {
            PSD + PlayerSaveData.PETS: PETS, PETS + List.SIZE: len(rows),
            PETS + List.ITEMS: ITEMS, ITEMS: ARRAY_CLASS,
            ARRAY_CLASS + Class.ELEMENT_CLASS: PET_CLASS,
            PET_CLASS + Class.NAME: NAME, PET_CLASS + Class.ELEMENT_CLASS: PET_CLASS,
            ITEMS + Array.MAX_LENGTH: len(rows),
        }
        self.bytes = {ITEMS + Array.DATA: struct.pack(f"<{len(rows)}Q", *[PET1, PET2][:len(rows)])}
        self.names = {NAME: "PetSaveData"}
        for ptr, (key, unlocked) in zip((PET1, PET2), rows, strict=False):
            self.mem[ptr] = PET_CLASS
            self.mem[ptr + PetSaveData.KEY] = key
            self.bytes[ptr + PetSaveData.IS_UNLOCK] = bytes([unlocked])

    def rptr(self, addr):
        return self.mem.get(addr)

    ri32 = rptr
    ru64 = rptr

    def read(self, addr, size):
        return self.bytes.get(addr)

    def read_cstr(self, addr):
        return self.names.get(addr)


def test_pet_capture_preserves_locked_and_unlocked_collection():
    assert read_pets(PetReader(), PSD) == [
        {"key": 101, "unlocked": True}, {"key": 102, "unlocked": False}]


def test_pet_capture_empty_requires_pet_element_type():
    reader = PetReader(())
    assert read_pets(reader, PSD) == []
    reader.names[NAME] = "RuneSaveData"
    assert read_pets(reader, PSD) is None


@pytest.mark.parametrize("address,value", [
    (PSD + PlayerSaveData.PETS, None), (PETS + List.SIZE, None),
    (PETS + List.SIZE, -1), (PETS + List.SIZE, 5001), (PETS + List.ITEMS, None),
    (ITEMS, None), (ARRAY_CLASS + Class.ELEMENT_CLASS, None),
    (PET_CLASS + Class.ELEMENT_CLASS, None), (ITEMS + Array.MAX_LENGTH, None),
    (ITEMS + Array.MAX_LENGTH, 1), (PET1, None),
    (PET1 + PetSaveData.KEY, None), (PET1 + PetSaveData.KEY, 0),
    (PET1 + PetSaveData.KEY, -1), (PET2 + PetSaveData.KEY, 101),
])
def test_pet_capture_rejects_missing_corrupt_or_duplicate_values(address, value):
    reader = PetReader()
    reader.mem[address] = value
    assert read_pets(reader, PSD) is None


@pytest.mark.parametrize("address,value", [
    (ITEMS + Array.DATA, None), (ITEMS + Array.DATA, b"\0"),
    (ITEMS + Array.DATA, struct.pack("<2Q", PET1, 0)),
    (PET2 + PetSaveData.IS_UNLOCK, None), (PET2 + PetSaveData.IS_UNLOCK, b"\x02"),
    (PET2 + PetSaveData.IS_UNLOCK, b""), (PET2 + PetSaveData.IS_UNLOCK, b"\x01\x00"),
])
def test_pet_capture_does_not_silently_drop_unreadable_rows(address, value):
    reader = PetReader()
    reader.bytes[address] = value
    assert read_pets(reader, PSD) is None


def test_pet_capture_rejects_wrong_row_type():
    reader = PetReader()
    reader.mem[PET2] = 0x40000
    assert read_pets(reader, PSD) is None


def test_pet_capture_rejects_list_changed_during_read():
    reader = PetReader()
    read = reader.read

    def changing_read(addr, size):
        reader.mem[PETS + List.SIZE] = 1
        return read(addr, size)

    reader.read = changing_read
    assert read_pets(reader, PSD) is None


def test_pet_capture_missing_save_and_exception_do_not_raise():
    assert read_pets(PetReader(), None) is None

    class RaisingReader:
        def rptr(self, addr):
            raise OSError("read failed")

    assert read_pets(RaisingReader(), PSD) is None
