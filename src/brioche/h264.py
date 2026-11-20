"""Small AVC framing adapter, shared by native and browser paths.

Encoders may expose Annex B or length-prefixed AVC. The wire format is Annex B;
IDR access units include parameter sets so a reset decoder can start immediately.
"""
import re

START = b"\x00\x00\x00\x01"
START_CODES = re.compile(b"\x00\x00(?:\x00)?\x01")


def nal_units(data):
    matches = list(START_CODES.finditer(data))
    return [data[m.end():matches[i+1].start() if i+1 < len(matches) else len(data)]
            for i, m in enumerate(matches) if m.end() < (matches[i+1].start() if i+1 < len(matches) else len(data))]


def avcc_parameters(extra):
    if not extra:
        return 4, []
    if extra[:1] != b"\x01":
        return 4, [unit for unit in nal_units(extra) if unit[0] & 31 in (7, 8)]
    if len(extra) < 7:
        raise ValueError("AVC 参数记录不完整")
    length_size = (extra[4] & 3) + 1
    position, parameters = 6, []
    counts = [extra[5] & 31]
    for group in range(2):
        for _ in range(counts[group]):
            if position + 2 > len(extra):
                raise ValueError("AVC 参数记录截断")
            size = int.from_bytes(extra[position:position+2], "big")
            position += 2
            if not size or position + size > len(extra):
                raise ValueError("AVC 参数集大小无效")
            parameters.append(extra[position:position+size])
            position += size
        if group == 0:
            if position >= len(extra):
                raise ValueError("AVC 缺少 PPS 数量")
            counts.append(extra[position])
            position += 1
    return length_size, parameters


def annexb_access_unit(data, extra=None, key=False):
    if not extra and data.startswith((b"\x00\x00\x01", START)):
        return data
    length_size, parameters = avcc_parameters(extra)
    if data.startswith((b"\x00\x00\x01", START)):
        units = nal_units(data)
    else:
        units, position = [], 0
        while position < len(data):
            if position + length_size > len(data):
                raise ValueError("AVC 访问单元截断")
            size = int.from_bytes(data[position:position+length_size], "big")
            position += length_size
            if not size or position + size > len(data):
                raise ValueError("AVC NAL 长度无效")
            units.append(data[position:position+size])
            position += size
    types = {unit[0] & 31 for unit in units}
    if (key or 5 in types) and parameters:
        units = [unit for unit in parameters if unit[0] & 31 not in types] + units
    return b"".join(START + unit for unit in units)


def access_unit_info(data):
    units = nal_units(data)
    types = {unit[0] & 31 for unit in units}
    sps = next((unit for unit in units if unit[0] & 31 == 7 and len(unit) >= 4), None)
    return {"key": 5 in types, "complete_key": {5, 7, 8}.issubset(types),
            "codec": "avc1." + sps[1:4].hex() if sps else None}
