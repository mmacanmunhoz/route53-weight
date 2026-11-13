#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.34", "pyyaml>=6"]
# ///
"""r53w - gerencia pesos (weighted routing) de registros alias na Route53.

Exemplos:
  r53w.py show                                  # todos os registros do config
  r53w.py show api
  r53w.py set api blue=90 green=10              # dry-run: so mostra o diff
  r53w.py set api blue=90 green=10 --apply
  r53w.py shift api --to green --step 10 --interval 60 --apply
  r53w.py shift api --to green --until 50 --apply   # para em 50%
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError

MAX_WEIGHT = 255


class R53WError(Exception):
    """Erro esperado, exibido ao usuario sem traceback."""


# --------------------------------------------------------------------------
# Funcoes puras (sem AWS)
# --------------------------------------------------------------------------

def fqdn(name: str) -> str:
    """Normaliza um nome DNS: minusculo, ponto final, sem escapes octais (\\052 -> *)."""
    name = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), name.strip().lower())
    return name if name.endswith(".") else name + "."


def alias_key(dns: str) -> str:
    """Forma comparavel do DNS de um LB (ignora 'dualstack.' e ponto final)."""
    return dns.strip().lower().rstrip(".").removeprefix("dualstack.")


def split_total(weights: dict[str, int], total: int = 100) -> dict[str, int]:
    """Distribui `total` proporcionalmente a `weights`, em inteiros (maior resto)."""
    base = sum(weights.values())
    if base <= 0:
        raise R53WError("nao da para calcular percentuais: a soma dos pesos e 0")
    exact = {k: w * total / base for k, w in weights.items()}
    out = {k: int(v) for k, v in exact.items()}
    leftover = total - sum(out.values())
    for k in sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)[:leftover]:
        out[k] += 1
    return out


def shift_plan(current: dict[str, int], to: str, step: int, until: int = 100) -> list[dict[str, int]]:
    """Sequencia de pesos (somando 100) que leva `to` do percentual atual ate `until`.

    Os demais alvos dividem o restante na mesma proporcao que tinham entre si.
    """
    pct = split_total(current)
    others = {k: w for k, w in current.items() if k != to}
    plan = []
    p = pct[to]
    while p < until:
        p = min(p + step, until)
        weights = split_total(others, 100 - p) if p < 100 else dict.fromkeys(others, 0)
        weights[to] = p
        plan.append({k: weights[k] for k in current})
    return plan


def fmt_weights(weights: dict[str, int]) -> str:
    return " ".join(f"{k}={w}" for k, w in weights.items())


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Target:
    ref: str                 # nome, ARN ou DNS do load balancer
    region: str | None = None


@dataclass
class Record:
    key: str
    name: str
    zone: str | None = None
    zone_id: str | None = None
    type: str = "A"
    evaluate_target_health: bool = True
    targets: dict[str, Target] = field(default_factory=dict)


def load_config(path: str) -> dict[str, Record]:
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        raise R53WError(f"config nao encontrado: {path} (use -c ou copie records.example.yaml)")
    except yaml.YAMLError as e:
        raise R53WError(f"YAML invalido em {path}: {e}")

    default_region = raw.get("region")
    records: dict[str, Record] = {}
    for key, body in (raw.get("records") or {}).items():
        where = f"records.{key}"
        if not isinstance(body, dict) or not body.get("name"):
            raise R53WError(f"{where}: campo 'name' obrigatorio")
        if not (body.get("zone") or body.get("zone_id")):
            raise R53WError(f"{where}: informe 'zone' (nome) ou 'zone_id'")
        raw_targets = body.get("targets") or {}
        if not isinstance(raw_targets, dict) or not raw_targets:
            raise R53WError(f"{where}: 'targets' precisa ter ao menos um load balancer")

        region = body.get("region", default_region)
        targets = {}
        for sid, t in raw_targets.items():
            if isinstance(t, str):
                targets[str(sid)] = Target(t, region)
            elif isinstance(t, dict) and t.get("lb"):
                targets[str(sid)] = Target(t["lb"], t.get("region", region))
            else:
                raise R53WError(f"{where}.targets.{sid}: use o nome do LB ou {{lb: ..., region: ...}}")

        records[str(key)] = Record(
            key=str(key),
            name=fqdn(body["name"]),
            zone=body.get("zone"),
            zone_id=body.get("zone_id"),
            type=str(body.get("type", "A")).upper(),
            evaluate_target_health=bool(body.get("evaluate_target_health", True)),
            targets=targets,
        )
    if not records:
        raise R53WError(f"{path}: nenhum registro em 'records'")
    return records


# --------------------------------------------------------------------------
# AWS
# --------------------------------------------------------------------------

@dataclass
class LoadBalancer:
    dns: str
    zone_id: str
    kind: str

    @property
    def alias_dns(self) -> str:
        # Mesmo formato que o console usa para ALB.
        prefix = "dualstack." if self.kind == "application" else ""
        return fqdn(prefix + self.dns)


class AWS:
    def __init__(self, profile: str | None, region: str | None):
        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.r53 = self.session.client("route53")
        self._elb: dict[str, object] = {}
        self._lbs: dict[tuple, LoadBalancer] = {}

    def _elbv2(self, region: str | None):
        region = region or self.session.region_name
        if not region:
            raise R53WError("regiao dos load balancers nao definida: use 'region' no config ou --region")
        if region not in self._elb:
            self._elb[region] = self.session.client("elbv2", region_name=region)
        return self._elb[region]

    def resolve_lb(self, target: Target) -> LoadBalancer:
        cache_key = (target.ref, target.region)
        if cache_key in self._lbs:
            return self._lbs[cache_key]

        ref, region = target.ref, target.region
        found = None
        try:
            if ref.startswith("arn:"):
                region = ref.split(":")[3]
                found = self._elbv2(region).describe_load_balancers(LoadBalancerArns=[ref])["LoadBalancers"]
            elif ".elb." in ref.lower():
                dns = alias_key(ref)
                m = re.search(r"\.([a-z]{2}(?:-[a-z]+)+-\d+)\.elb\.|\.elb\.([a-z]{2}(?:-[a-z]+)+-\d+)\.", dns)
                if m:
                    region = m.group(1) or m.group(2)
                pages = self._elbv2(region).get_paginator("describe_load_balancers").paginate()
                found = [lb for page in pages for lb in page["LoadBalancers"] if lb["DNSName"].lower() == dns]
            else:
                found = self._elbv2(region).describe_load_balancers(Names=[ref])["LoadBalancers"]
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("LoadBalancerNotFound", "LoadBalancerNotFoundException"):
                raise
        if not found:
            raise R53WError(f"load balancer '{ref}' nao encontrado (regiao {region or self.session.region_name})")

        lb = LoadBalancer(found[0]["DNSName"], found[0]["CanonicalHostedZoneId"], found[0].get("Type", "application"))
        self._lbs[cache_key] = lb
        return lb

    def resolve_zone(self, rec: Record) -> str:
        if rec.zone_id:
            return rec.zone_id.split("/")[-1]
        name = fqdn(rec.zone)
        zones = self.r53.list_hosted_zones_by_name(DNSName=name)["HostedZones"]
        matches = [z for z in zones if fqdn(z["Name"]) == name]
        if not matches:
            raise R53WError(f"hosted zone '{name}' nao encontrada")
        if len(matches) > 1:
            ids = ", ".join(
                f"{z['Id'].split('/')[-1]} ({'privada' if z['Config'].get('PrivateZone') else 'publica'})"
                for z in matches
            )
            raise R53WError(f"mais de uma hosted zone '{name}': {ids}. Defina 'zone_id' no config.")
        return matches[0]["Id"].split("/")[-1]

    def read_rrsets(self, zone_id: str, rec: Record) -> list[dict]:
        """Todos os record sets com o nome e tipo do registro."""
        out = []
        pages = self.r53.get_paginator("list_resource_record_sets").paginate(
            HostedZoneId=zone_id, StartRecordName=rec.name, StartRecordType=rec.type
        )
        for page in pages:
            for rr in page["ResourceRecordSets"]:
                if fqdn(rr["Name"]) != rec.name:
                    return out
                if rr["Type"] == rec.type:
                    out.append(rr)
        return out

    def apply(self, zone_id: str, changes: list[dict], comment: str) -> None:
        resp = self.r53.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch={"Comment": comment, "Changes": changes}
        )
        print("  aplicado, aguardando propagacao (INSYNC)...", flush=True)
        self.r53.get_waiter("resource_record_sets_changed").wait(
            Id=resp["ChangeInfo"]["Id"], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
        )
        print("  INSYNC")


# --------------------------------------------------------------------------
# Estado e diff
# --------------------------------------------------------------------------

@dataclass
class State:
    zone_id: str
    weighted: dict[str, dict]      # SetIdentifier -> rrset atual
    lbs: dict[str, LoadBalancer]   # alvo do config -> LB resolvido

    def weights(self, rec: Record) -> dict[str, int]:
        """Pesos atuais dos alvos do config (0 se o registro ainda nao existe)."""
        return {sid: self.weighted.get(sid, {}).get("Weight", 0) for sid in rec.targets}

    def foreign(self, rec: Record) -> dict[str, int]:
        """Registros ponderados que existem na Route53 mas nao estao no config."""
        return {sid: rr["Weight"] for sid, rr in self.weighted.items() if sid not in rec.targets}


def load_state(aws: AWS, rec: Record) -> State:
    zone_id = aws.resolve_zone(rec)
    rrsets = aws.read_rrsets(zone_id, rec)
    conflicts = [rr for rr in rrsets if "Weight" not in rr]
    if conflicts:
        policy = "simples" if "SetIdentifier" not in conflicts[0] else "com outra politica de roteamento"
        raise R53WError(
            f"{rec.name} ({rec.type}) ja existe como registro {policy}. "
            "A Route53 nao permite misturar com weighted: remova/converta esse registro antes."
        )
    lbs = {sid: aws.resolve_lb(t) for sid, t in rec.targets.items()}
    return State(zone_id, {rr["SetIdentifier"]: rr for rr in rrsets}, lbs)


def build_changes(rec: Record, state: State, new_weights: dict[str, int]) -> tuple[list[dict], list[str]]:
    """UPSERTs necessarios para chegar em `new_weights` + avisos sobre troca de alvo."""
    changes, notes = [], []
    for sid, weight in new_weights.items():
        lb = state.lbs[sid]
        current = state.weighted.get(sid)
        desired = {
            "Name": rec.name,
            "Type": rec.type,
            "SetIdentifier": sid,
            "Weight": weight,
            "AliasTarget": {
                "HostedZoneId": lb.zone_id,
                "DNSName": lb.alias_dns,
                "EvaluateTargetHealth": rec.evaluate_target_health,
            },
        }
        if current:
            if "HealthCheckId" in current:
                desired["HealthCheckId"] = current["HealthCheckId"]
            cur_alias = current.get("AliasTarget")
            same_target = bool(cur_alias) and alias_key(cur_alias["DNSName"]) == alias_key(lb.dns)
            same_eth = bool(cur_alias) and cur_alias["EvaluateTargetHealth"] == rec.evaluate_target_health
            if not same_target:
                old = alias_key(cur_alias["DNSName"]) if cur_alias else "registro nao-alias"
                notes.append(f"{sid}: alvo muda de {old} para {alias_key(lb.dns)}")
            elif not same_eth:
                notes.append(f"{sid}: EvaluateTargetHealth passa a ser {rec.evaluate_target_health}")
            if current["Weight"] == weight and same_target and same_eth:
                continue
        else:
            notes.append(f"{sid}: registro sera criado apontando para {alias_key(lb.dns)}")
        changes.append({"Action": "UPSERT", "ResourceRecordSet": desired})
    return changes, notes


def pct(weight: int, total: int) -> str:
    return f"{weight * 100 / total:5.1f}%" if total else "    -"


def print_diff(rec: Record, state: State, new_weights: dict[str, int]) -> None:
    before = {**state.weights(rec), **state.foreign(rec)}
    after = {**before, **new_weights}
    tb, ta = sum(before.values()), sum(after.values())
    width = max(len(s) for s in after)
    for sid in after:
        mark = "  " if before[sid] == after[sid] else "->"
        extra = "  (fora do config)" if sid not in rec.targets else ""
        print(f"  {sid:<{width}}  {before[sid]:>3} {mark} {after[sid]:>3}   {pct(before[sid], tb)} {mark} {pct(after[sid], ta)}{extra}")


def warn(msg: str) -> None:
    print(f"  AVISO: {msg}")


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------

def cmd_show(aws: AWS, records: dict[str, Record], args) -> int:
    keys = [args.record] if args.record else list(records)
    for key in keys:
        rec = get_record(records, key)
        state = load_state(aws, rec)
        print(f"{rec.key}: {rec.name} {rec.type}  (zona {state.zone_id})")
        weights = {**state.weights(rec), **state.foreign(rec)}
        total = sum(weights.values())
        width = max(len(s) for s in weights)
        for sid, w in weights.items():
            rr = state.weighted.get(sid)
            if rr is None:
                detail = f"AUSENTE na Route53 (config: {alias_key(state.lbs[sid].dns)})"
            else:
                alias = rr.get("AliasTarget")
                detail = alias_key(alias["DNSName"]) if alias else "registro nao-alias"
                if alias and not alias["EvaluateTargetHealth"]:
                    detail += "  [health desligado]"
                if sid not in rec.targets:
                    detail += "  (fora do config)"
                elif not alias or alias_key(alias["DNSName"]) != alias_key(state.lbs[sid].dns):
                    detail += f"  [DIFERE do config: {alias_key(state.lbs[sid].dns)}]"
            print(f"  {sid:<{width}}  {w:>3}  {pct(w, total)}  {detail}")
        if weights and total == 0:
            warn("todos os pesos sao 0: a Route53 distribui igualmente entre todos os registros")
        print()
    return 0


def cmd_set(aws: AWS, records: dict[str, Record], args) -> int:
    rec = get_record(records, args.record)
    new_weights: dict[str, int] = {}
    for item in args.weights:
        sid, sep, value = item.partition("=")
        if not sep or not value.isdigit() or int(value) > MAX_WEIGHT:
            raise R53WError(f"peso invalido '{item}': use alvo=N com N entre 0 e {MAX_WEIGHT}")
        if sid not in rec.targets:
            raise R53WError(f"alvo '{sid}' nao existe em '{rec.key}' (alvos: {', '.join(rec.targets)})")
        new_weights[sid] = int(value)

    state = load_state(aws, rec)
    changes, notes = build_changes(rec, state, new_weights)
    print(f"{rec.key}: {rec.name} {rec.type}")
    print_diff(rec, state, new_weights)
    report(rec, state, new_weights, notes)

    if not changes:
        print("  nada a alterar.")
        return 0
    if not args.apply:
        print("  dry-run: nada foi alterado. Repita com --apply para aplicar.")
        return 0
    aws.apply(state.zone_id, changes, f"r53w set {rec.key} {fmt_weights(new_weights)}")
    return 0


def cmd_shift(aws: AWS, records: dict[str, Record], args) -> int:
    rec = get_record(records, args.record)
    if args.to not in rec.targets:
        raise R53WError(f"alvo '{args.to}' nao existe em '{rec.key}' (alvos: {', '.join(rec.targets)})")
    if len(rec.targets) < 2:
        raise R53WError("shift precisa de ao menos 2 alvos no config")
    if not 1 <= args.step <= 100 or not 1 <= args.until <= 100:
        raise R53WError("--step e --until devem estar entre 1 e 100")

    state = load_state(aws, rec)
    foreign = {sid: w for sid, w in state.foreign(rec).items() if w > 0}
    if foreign:
        raise R53WError(
            f"ha registros com peso fora do config ({fmt_weights(foreign)}); "
            "o shift nao conseguiria garantir os percentuais. Adicione-os ao config ou zere-os."
        )
    original = state.weights(rec)
    if sum(original.values()) == 0:
        raise R53WError("todos os pesos atuais sao 0; defina um ponto de partida com 'set' primeiro")

    plan = shift_plan(original, args.to, args.step, args.until)
    print(f"{rec.key}: {rec.name} {rec.type}")
    print(f"  atual:    {fmt_weights(original)}")
    if not plan:
        print(f"  '{args.to}' ja esta em {split_total(original)[args.to]}% (>= {args.until}%). Nada a fazer.")
        return 0
    for i, weights in enumerate(plan, 1):
        print(f"  passo {i:>2}: {fmt_weights(weights)}")
    total_wait = args.interval * (len(plan) - 1)
    print(f"  {len(plan)} passo(s), {args.interval}s entre eles (~{total_wait}s de espera no total)")
    if not args.apply:
        print("  dry-run: nada foi alterado. Repita com --apply para aplicar.")
        return 0

    rollback = f"{Path(sys.argv[0]).name} set {rec.key} {fmt_weights(original)} --apply"
    expected = original
    try:
        for i, weights in enumerate(plan, 1):
            if i > 1:
                time.sleep(args.interval)
                state = load_state(aws, rec)
                if state.weights(rec) != expected:
                    raise R53WError(
                        f"os pesos mudaram por fora durante o shift (esperado {fmt_weights(expected)}, "
                        f"encontrado {fmt_weights(state.weights(rec))}). Abortando sem sobrescrever."
                    )
            print(f"passo {i}/{len(plan)}: {fmt_weights(weights)}", flush=True)
            changes, notes = build_changes(rec, state, weights)
            for note in notes:
                warn(note)
            if changes:
                aws.apply(state.zone_id, changes, f"r53w shift {rec.key} -> {args.to} ({i}/{len(plan)})")
            expected = weights
    except KeyboardInterrupt:
        print(f"\ninterrompido. Pesos atuais: {fmt_weights(expected)}")
        print(f"para voltar ao estado inicial: {rollback}")
        return 130
    except (R53WError, ClientError, BotoCoreError):
        print(f"shift interrompido. Ultimo estado aplicado por este comando: {fmt_weights(expected)}")
        print(f"para voltar ao estado inicial: {rollback}")
        raise
    print(f"concluido: {fmt_weights(expected)}")
    return 0


def report(rec: Record, state: State, new_weights: dict[str, int], notes: list[str]) -> None:
    for note in notes:
        warn(note)
    final = {**state.weights(rec), **state.foreign(rec), **new_weights}
    if sum(final.values()) == 0:
        warn("todos os pesos ficam 0: a Route53 distribui igualmente entre todos os registros")
    if state.foreign(rec):
        warn(f"existem registros fora do config que tambem recebem trafego: {fmt_weights(state.foreign(rec))}")


def get_record(records: dict[str, Record], key: str) -> Record:
    if key not in records:
        raise R53WError(f"registro '{key}' nao esta no config (disponiveis: {', '.join(records)})")
    return records[key]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="r53w", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("-c", "--config", default="records.yaml", help="arquivo de config (default: records.yaml)")
    p.add_argument("--profile", help="profile da AWS")
    p.add_argument("--region", help="regiao dos load balancers (sobrepoe o default da sessao)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("show", help="mostra os pesos atuais")
    s.add_argument("record", nargs="?", help="registro do config (default: todos)")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("set", help="define pesos (alvos omitidos nao mudam)")
    s.add_argument("record")
    s.add_argument("weights", nargs="+", metavar="alvo=peso", help=f"peso relativo de 0 a {MAX_WEIGHT}")
    s.add_argument("--apply", action="store_true", help="aplica de verdade (sem isso e dry-run)")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("shift", help="migra trafego gradualmente para um alvo")
    s.add_argument("record")
    s.add_argument("--to", required=True, help="alvo que vai receber o trafego")
    s.add_argument("--step", type=int, default=10, help="pontos percentuais por passo (default: 10)")
    s.add_argument("--until", type=int, default=100, help="percentual final do alvo (default: 100)")
    s.add_argument("--interval", type=int, default=60, help="segundos entre passos (default: 60)")
    s.add_argument("--apply", action="store_true", help="aplica de verdade (sem isso e dry-run)")
    s.set_defaults(func=cmd_shift)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = load_config(args.config)
        aws = AWS(args.profile, args.region)
        return args.func(aws, records, args)
    except R53WError as e:
        print(f"erro: {e}", file=sys.stderr)
    except (ClientError, BotoCoreError) as e:
        print(f"erro da AWS: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
