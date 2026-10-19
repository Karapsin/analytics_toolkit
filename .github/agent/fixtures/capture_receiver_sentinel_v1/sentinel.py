"""Fixed inert receiver-platform sentinel; never a product visual check."""
import json
import os
import sys


def main():
    value = json.loads(sys.argv[1])
    if set(value) != {"nonce", "trusted_head", "purpose"} or value["purpose"] != "receiver-rehearsal":
        raise ValueError("Invalid fixed sentinel identity")
    result = dict(value, marker="receiver-rehearsal-v1", visual_approval=False,
                  prerequisite_satisfied=False)
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open("/work/sentinel-result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    print(raw.decode())


if __name__ == "__main__":
    main()
