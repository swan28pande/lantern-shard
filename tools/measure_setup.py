"""Per test class: CPU of importing its module (sqlglot preloaded) + setUpClass, in a fresh fork."""
import importlib, json, os, resource, sys
sys.path.insert(0, "/app")
import runner
tests = json.load(open(sys.argv[1]))["tests"]
classes = sorted({(t["file"][:-3].replace("/", "."), t["id"].split("::")[1]) for t in tests if t["id"].count("::") == 2})
runner.preload(full=True)
res = {}
for mod, cls in classes:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        ru0 = resource.getrusage(resource.RUSAGE_SELF)
        m = importlib.import_module(mod)
        ru1 = resource.getrusage(resource.RUSAGE_SELF)
        k = getattr(m, cls)
        if "ProcessPoolExecutor" in vars(m):
            m.ProcessPoolExecutor = runner._sync_executor_class()
        k.setUpClass()
        ru2 = resource.getrusage(resource.RUSAGE_SELF)
        f = lambda a, b: (b.ru_utime - a.ru_utime) + (b.ru_stime - a.ru_stime)
        os.write(w, json.dumps([f(ru0, ru1), f(ru1, ru2)]).encode())
        os._exit(0)
    os.close(w)
    data = os.read(r, 1000)
    os.waitpid(pid, 0)
    res[f"{mod}::{cls}"] = json.loads(data)
json.dump(res, open(sys.argv[2], "w"), indent=1)
for k, v in sorted(res.items(), key=lambda x: -sum(x[1]))[:12]:
    print(k, [round(x, 3) for x in v])
