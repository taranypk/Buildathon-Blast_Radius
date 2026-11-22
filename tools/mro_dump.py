"""Import every sqlglot module and dump class -> MRO (sqlglot classes only)."""
import importlib, json, pkgutil, sys, inspect
import sqlglot
out = {}
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        mod = importlib.import_module(info.name)
    except Exception as e:
        print("skip", info.name, e, file=sys.stderr); continue
for name, mod in list(sys.modules.items()):
    if not name.startswith("sqlglot"):
        continue
    stack = [v for v in vars(mod).values() if inspect.isclass(v)]
    while stack:
        c = stack.pop()
        if not c.__module__.startswith("sqlglot"):
            continue
        k = f"{c.__module__}:{c.__qualname__}"
        if k in out:
            continue
        out[k] = [f"{b.__module__}:{b.__qualname__}" for b in c.__mro__ if b.__module__.startswith("sqlglot")]
        stack += [v for v in vars(c).values() if inspect.isclass(v)]
json.dump(out, open(sys.argv[1], "w"))
print(len(out), "classes")
