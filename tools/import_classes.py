"""Classes instantiated while importing sqlglot and all of its modules (dialects, parsers,
generators, ...): a change to one of these can break imports."""
import sys, json, importlib, pkgutil
seen = set()


def prof(frame, event, arg):
    if event == "call" and frame.f_code.co_name == "__init__":
        self = frame.f_locals.get("self")
        if self is not None:
            c = type(self)
            if c.__module__.startswith("sqlglot"):
                seen.add(c)


sys.setprofile(prof)
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception as e:
        print("skip", info.name, e, file=sys.stderr)
sys.setprofile(None)
out = sorted({f"{b.__module__}:{b.__qualname__}" for c in seen for b in [c]})
json.dump(out, open(sys.argv[1], "w"))
print(len(out), "classes instantiated at import")
