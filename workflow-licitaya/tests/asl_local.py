"""Intérprete mínimo de Amazon States Language para probar el workflow localmente.

Soporta lo que usa licitaya-evaluador-oportunidades: Task (Lambda por ARN, dynamodb:putItem,
dynamodb:updateItem, sns:publish), Pass, Choice, Map (INLINE), Succeed, Fail, Catch,
ResultPath, Parameters/ItemSelector con ".$", States.JsonMerge y States.Format.
Retry se ignora (los fallos van directo a Catch). No reemplaza a Step Functions:
sirve para validar la lógica y el flujo de datos antes de desplegar.
"""
import copy
import json
import re
from typing import Any, Callable, Dict, List

import boto3


class StatesError(Exception):
    def __init__(self, error: str, cause: str):
        super().__init__(f"{error}: {cause}")
        self.error, self.cause = error, cause


def _get(path: str, data: Any, ctx: Dict[str, Any]) -> Any:
    if path.startswith("$$"):
        cur, rest = ctx, path[2:]
    elif path.startswith("$"):
        cur, rest = data, path[1:]
    else:
        raise ValueError(f"Ruta inválida: {path}")
    for part in [p for p in rest.split(".") if p]:
        if not isinstance(cur, dict) or part not in cur:
            raise StatesError("States.Runtime", f"La ruta {path} no existe")
        cur = cur[part]
    return cur


def _set(data: Any, path: Any, value: Any) -> Any:
    if path is None:
        return data
    if path == "$":
        return value
    out = copy.deepcopy(data)
    cur = out
    parts = [p for p in path[1:].split(".") if p]
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return out


def _intrinsic(expr: str, data: Any, ctx: Dict[str, Any]) -> Any:
    m = re.match(r"^States\.(\w+)\((.*)\)$", expr.strip(), re.S)
    name, args_txt = m.group(1), m.group(2)
    args: List[Any] = []
    for raw in re.findall(r"'[^']*'|[^,]+", args_txt):
        raw = raw.strip()
        if raw.startswith("'"):
            args.append(raw[1:-1])
        elif raw.startswith("$"):
            args.append(_get(raw, data, ctx))
        elif raw in ("true", "false"):
            args.append(raw == "true")
        else:
            args.append(json.loads(raw))
    if name == "JsonMerge":
        merged = dict(args[0])
        merged.update(args[1])
        return merged
    if name == "Format":
        template, values = args[0], args[1:]
        for v in values:
            template = template.replace("{}", str(v), 1)
        return template
    raise NotImplementedError(name)


def _params(tpl: Any, data: Any, ctx: Dict[str, Any]) -> Any:
    if isinstance(tpl, dict):
        out = {}
        for k, v in tpl.items():
            if k.endswith(".$"):
                out[k[:-2]] = _intrinsic(v, data, ctx) if v.startswith("States.") else _get(v, data, ctx)
            else:
                out[k] = _params(v, data, ctx)
        return out
    if isinstance(tpl, list):
        return [_params(v, data, ctx) for v in tpl]
    return tpl


_OPS: Dict[str, Callable[[Any, Any], bool]] = {
    "NumericLessThan": lambda a, b: isinstance(a, (int, float)) and not isinstance(a, bool) and a < b,
    "NumericGreaterThan": lambda a, b: isinstance(a, (int, float)) and not isinstance(a, bool) and a > b,
    "NumericGreaterThanEquals": lambda a, b: isinstance(a, (int, float)) and not isinstance(a, bool) and a >= b,
    "BooleanEquals": lambda a, b: isinstance(a, bool) and a == b,
    "IsNull": lambda a, b: (a is None) == b,
}


def _rule(rule: Dict[str, Any], data: Any, ctx: Dict[str, Any]) -> bool:
    if "And" in rule:
        return all(_rule(r, data, ctx) for r in rule["And"])
    if "Or" in rule:
        return any(_rule(r, data, ctx) for r in rule["Or"])
    if "Not" in rule:
        return not _rule(rule["Not"], data, ctx)
    value = _get(rule["Variable"], data, ctx)
    for op, fn in _OPS.items():
        if op in rule:
            return fn(value, rule[op])
    raise NotImplementedError(rule)


class Machine:
    def __init__(self, definition: Dict[str, Any], lambdas: Dict[str, Callable], trace: List[str] = None):
        self.defn, self.lambdas = definition, lambdas
        self.trace = trace if trace is not None else []
        self.ddb, self.sns = boto3.client("dynamodb"), boto3.client("sns")

    def _task(self, st: Dict[str, Any], data: Any, ctx: Dict[str, Any]) -> Any:
        res = st["Resource"]
        payload = _params(st["Parameters"], data, ctx) if "Parameters" in st else data
        if res in self.lambdas:
            out = self.lambdas[res](json.loads(json.dumps(payload)), None)
            return json.loads(json.dumps(out))  # Step Functions serializa la salida
        if res == "arn:aws:states:::dynamodb:putItem":
            self.ddb.put_item(**payload)
            return {}
        if res == "arn:aws:states:::dynamodb:updateItem":
            self.ddb.update_item(**payload)
            return {}
        if res == "arn:aws:states:::sns:publish":
            return self.sns.publish(**payload)
        raise NotImplementedError(res)

    def run(self, data: Any, defn: Dict[str, Any] = None, ctx: Dict[str, Any] = None) -> Any:
        defn = defn or self.defn
        ctx = ctx or {}
        name = defn["StartAt"]
        while True:
            st = defn["States"][name]
            self.trace.append(name)
            kind = st["Type"]
            try:
                if kind == "Task":
                    data = _set(data, st.get("ResultPath", "$"), self._task(st, data, ctx))
                elif kind == "Pass":
                    result = _params(st["Parameters"], data, ctx) if "Parameters" in st else st.get("Result", data)
                    data = _set(data, st.get("ResultPath", "$"), result)
                elif kind == "Choice":
                    nxt = next((r["Next"] for r in st["Choices"] if _rule(r, data, ctx)), st.get("Default"))
                    if nxt is None:
                        raise StatesError("States.NoChoiceMatched", name)
                    name = nxt
                    continue
                elif kind == "Map":
                    items = _get(st["ItemsPath"], data, ctx)
                    results = []
                    for idx, item in enumerate(items):
                        mctx = {"Map": {"Item": {"Index": idx, "Value": item}}}
                        sub_in = _params(st["ItemSelector"], data, mctx) if "ItemSelector" in st else item
                        results.append(self.run(sub_in, st["ItemProcessor"], mctx))
                    data = _set(data, st.get("ResultPath", "$"), results)
                elif kind == "Succeed":
                    return data
                elif kind == "Fail":
                    raise StatesError(st.get("Error", "Fail"), st.get("Cause", ""))
                else:
                    raise NotImplementedError(kind)
            except StatesError as err:
                if kind == "Fail":
                    raise
                catch = next((c for c in st.get("Catch", []) if "States.ALL" in c["ErrorEquals"] or err.error in c["ErrorEquals"]), None)
                if not catch:
                    raise
                data = _set(data, catch.get("ResultPath", "$"), {"Error": err.error, "Cause": err.cause})
                name = catch["Next"]
                continue
            except Exception as exc:  # error de la Lambda / servicio
                catch = next((c for c in st.get("Catch", []) if "States.ALL" in c["ErrorEquals"]), None)
                if not catch:
                    raise
                data = _set(data, catch.get("ResultPath", "$"), {"Error": type(exc).__name__, "Cause": str(exc)})
                name = catch["Next"]
                continue
            if st.get("End"):
                return data
            name = st["Next"]
