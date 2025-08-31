import logging, json, sys
class JsonFormatter(logging.Formatter):
    def format(self, r): return json.dumps({"lvl":r.levelname,"msg":r.getMessage(),"logger":r.name}, ensure_ascii=False)
def setup_root_logger(level=logging.INFO):
    lg=logging.getLogger(); lg.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in lg.handlers):
        h=logging.StreamHandler(sys.stdout); h.setFormatter(JsonFormatter()); lg.addHandler(h)
    return lg
