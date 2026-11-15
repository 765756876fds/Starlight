"""Read GGUF headers/tensor extents without loading model weights or executing code."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import struct


SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?",
           10: "Q", 11: "q", 12: "d"}
BLOCKS = {0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22),
          7: (32, 24), 8: (32, 34), 10: (256, 84), 11: (256, 110),
          12: (256, 144), 13: (256, 176), 14: (256, 210), 15: (256, 292), 30: (1, 2)}


def inspect(path, *, hash_weights=False):
    path = Path(path)
    with path.open("rb") as stream:
        def number(fmt):
            size = struct.calcsize("<" + fmt)
            raw = stream.read(size)
            if len(raw) != size:
                raise ValueError("truncated GGUF header")
            return struct.unpack("<" + fmt, raw)[0]
        def string():
            size = number("Q")
            if size > 16_000_000:
                raise ValueError("invalid GGUF string length")
            raw = stream.read(size)
            if len(raw) != size:
                raise ValueError("truncated GGUF string")
            return raw.decode("utf-8")
        def value(kind):
            if kind == 8:
                return string()
            if kind == 9:
                subtype, count = number("I"), number("Q")
                if count > 2_000_000:
                    raise ValueError("GGUF array too large")
                # Read, but don't retain vocabulary/merge tables in reports.
                for _ in range(count):
                    value(subtype)
                return {"array_type": subtype, "length": count}
            return number(SCALARS[kind])
        if stream.read(4) != b"GGUF":
            raise ValueError("not GGUF")
        version, tensors, pairs = number("I"), number("Q"), number("Q")
        if version not in (2, 3) or tensors > 100_000 or pairs > 100_000:
            raise ValueError("unsupported GGUF header")
        metadata = {}
        for _ in range(pairs):
            key, kind = string(), number("I")
            metadata[key] = value(kind)
        ends, types, unknown = [], Counter(), []
        parameters = 0
        for _ in range(tensors):
            name, nd = string(), number("I")
            if nd > 8:
                raise ValueError("invalid tensor dimensions")
            dims = [number("Q") for _ in range(nd)]
            kind, offset = number("I"), number("Q")
            count = math.prod(dims)
            parameters += count
            types[kind] += 1
            if kind in BLOCKS:
                block, size = BLOCKS[kind]
                if count % block:
                    raise ValueError("invalid quantization block")
                ends.append(offset + count // block * size)
            else:
                unknown.append({"name": name, "type": kind})
        alignment = metadata.get("general.alignment", 32)
        start = (stream.tell() + alignment - 1) // alignment * alignment
    size = path.stat().st_size
    result = {"file": path.name, "bytes": size, "gguf_version": version,
              "tensor_count": tensors, "parameter_count": parameters,
              "tensor_types": dict(types), "metadata": metadata,
              "data_offset": start, "known_tensor_end": start + max(ends, default=0),
              "known_tensor_bounds_valid": start + max(ends, default=0) <= size,
              "unknown_tensor_types": unknown}
    if hash_weights:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hash", action="store_true")
    args = parser.parse_args()
    rows = [inspect(path, hash_weights=args.hash) for path in sorted(args.directory.rglob("*.gguf"))]
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(row["file"], row["metadata"].get("general.architecture"),
              row["metadata"].get("general.file_type"), row["known_tensor_bounds_valid"])


if __name__ == "__main__":
    main()
