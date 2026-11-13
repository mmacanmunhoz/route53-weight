#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.34", "pyyaml>=6"]
# ///
"""r53w - gerencia pesos (weighted routing) de registros alias na Route53.

Exemplos:
  r53w.py show                                  # todos os registros do config
  r53w.py check api                             # testa cada ALB direto, sem passar pelo DNS
  r53w.py set api blue=90 green=10              # dry-run: so mostra o diff
  r53w.py set api blue=90 green=10 --apply
  r53w.py set api blue=100 green=0 --replace-simple --apply   # converte registro simples
  r53w.py shift api --to green --step 10 --interval 60 --apply
  r53w.py shift api --to green --until 50 --apply             # para em 50%
  r53w.py simplify api --to green --apply                     # volta a ser registro simples
"""
from __future__ import annotations

import argparse
import http.client
import re
import socket
import ssl
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


def rr_target(rr: dict) -> str:
    """Para onde um record set aponta, em forma legivel/comparavel."""
    if "AliasTarget" in rr:
        return alias_key(rr["AliasTarget"]["DNSName"])
    return ", ".join(v["Value"] for v in rr.get("ResourceRecords", []))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Target:
    ref: str | None = None            # nome, ARN ou DNS do LB (consulta a API do ELB)
    region: str | None = None
    dns: str | None = None            # modo direto: DNS + hosted zone id, sem consultar o ELB
    hosted_zone_id: str | None = None
    ipv6: bool = False                # so no modo direto; no outro vem do proprio LB


@dataclass
class Check:
    path: str = "/"
    scheme: str = "https"
    port: int | None = None
    host: str | None = None          # default: o nome do registro
    expect: list[int] | None = None  # default: qualquer status 2xx/3xx
    timeout: float = 5.0


@dataclass
class Record:
    key: str
    name: str
    zone: str | None = None
    zone_id: str | None = None
    type: str = "A"
    evaluate_target_health: bool = True
    profile: str | None = None       # conta dos load balancers
    dns_profile: str | None = None   # conta da hosted zone (default: a mesma)
    check: Check = field(default_factory=Check)
    targets: dict[str, Target] = field(default_factory=dict)


def load_config(path: str) -> dict[str, Record]:
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        raise R53WError(f"config nao encontrado: {path} (use -c ou copie records.example.yaml)")
    except yaml.YAMLError as e:
        raise R53WError(f"YAML invalido em {path}: {e}")

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

        region = body.get("region", raw.get("region"))
        targets = {}
        for sid, t in raw_targets.items():
            if isinstance(t, str):
                targets[str(sid)] = Target(t, region)
            elif isinstance(t, dict) and t.get("lb"):
                targets[str(sid)] = Target(t["lb"], t.get("region", region))
            elif isinstance(t, dict) and t.get("dns") and t.get("hosted_zone_id"):
                targets[str(sid)] = Target(
                    dns=t["dns"], hosted_zone_id=str(t["hosted_zone_id"]), ipv6=bool(t.get("ipv6", False))
                )
            else:
                raise R53WError(
                    f"{where}.targets.{sid}: use o nome do LB, {{lb: ..., region: ...}} "
                    "ou {dns: ..., hosted_zone_id: ...}"
                )

        raw_check = {**(raw.get("check") or {}), **(body.get("check") or {})}
        unknown = set(raw_check) - set(Check.__dataclass_fields__)
        if unknown:
            raise R53WError(f"{where}.check: campos desconhecidos: {', '.join(sorted(unknown))}")
        expect = raw_check.get("expect")
        if expect is not None:
            raw_check["expect"] = [int(c) for c in (expect if isinstance(expect, list) else [expect])]
        check = Check(**raw_check)
        if check.scheme not in ("http", "https"):
            raise R53WError(f"{where}.check.scheme: use http ou https")

        records[str(key)] = Record(
            key=str(key),
            name=fqdn(body["name"]),
            zone=body.get("zone"),
            zone_id=body.get("zone_id"),
            type=str(body.get("type", "A")).upper(),
            evaluate_target_health=bool(body.get("evaluate_target_health", True)),
            profile=body.get("profile", raw.get("profile")),
            dns_profile=body.get("dns_profile", raw.get("dns_profile")),
            check=check,
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
    scheme: str
    state: str
    ipv6: bool

    @property
    def alias_dns(self) -> str:
        # Mesmo formato que o console usa para ALB.
        prefix = "dualstack." if self.kind == "application" else ""
        return fqdn(prefix + self.dns)


class AWS:
    """Clientes boto3. Route53 e ELB podem estar em contas (profiles) diferentes."""

    def __init__(self, profile: str | None, dns_profile: str | None, region: str | None):
        self.cli_profile, self.cli_dns_profile, self.cli_region = profile, dns_profile, region
        self._sessions: dict[str | None, boto3.Session] = {}
        self._clients: dict[tuple, object] = {}
        self._lbs: dict[tuple, LoadBalancer] = {}

    def _session(self, profile: str | None) -> boto3.Session:
        if profile not in self._sessions:
            self._sessions[profile] = boto3.Session(profile_name=profile, region_name=self.cli_region)
        return self._sessions[profile]

    def _client(self, service: str, profile: str | None, region: str | None = None):
        key = (service, profile, region)
        if key not in self._clients:
            self._clients[key] = self._session(profile).client(service, region_name=region)
        return self._clients[key]

    def lb_profile(self, rec: Record) -> str | None:
        return self.cli_profile or rec.profile

    def dns_profile(self, rec: Record) -> str | None:
        return self.cli_dns_profile or rec.dns_profile or self.lb_profile(rec)

    def r53(self, rec: Record):
        return self._client("route53", self.dns_profile(rec))

    def _elbv2(self, rec: Record, region: str | None):
        profile = self.lb_profile(rec)
        region = region or self._session(profile).region_name
        if not region:
            raise R53WError("regiao dos load balancers nao definida: use 'region' no config ou --region")
        return self._client("elbv2", profile, region), region

    def resolve_lb(self, rec: Record, target: Target) -> LoadBalancer:
        if target.dns:
            # Modo direto: nada de API do ELB. O DNS de NLB tem a forma nome.elb.<regiao>.amazonaws.com.
            dns = alias_key(target.dns)
            kind = "network" if re.search(r"\.elb\.[a-z0-9-]+\.amazonaws\.com", dns) else "application"
            return LoadBalancer(dns, target.hosted_zone_id, kind, "?", "active", target.ipv6)

        cache_key = (self.lb_profile(rec), target.ref, target.region)
        if cache_key in self._lbs:
            return self._lbs[cache_key]

        ref, region = target.ref, target.region
        found = None
        try:
            if ref.startswith("arn:"):
                elb, region = self._elbv2(rec, ref.split(":")[3])
                found = elb.describe_load_balancers(LoadBalancerArns=[ref])["LoadBalancers"]
            elif ".elb." in ref.lower():
                dns = alias_key(ref)
                m = re.search(r"\.([a-z]{2}(?:-[a-z]+)+-\d+)\.elb\.|\.elb\.([a-z]{2}(?:-[a-z]+)+-\d+)\.", dns)
                elb, region = self._elbv2(rec, (m.group(1) or m.group(2)) if m else region)
                pages = elb.get_paginator("describe_load_balancers").paginate()
                found = [lb for page in pages for lb in page["LoadBalancers"] if lb["DNSName"].lower() == dns]
            else:
                elb, region = self._elbv2(rec, region)
                found = elb.describe_load_balancers(Names=[ref])["LoadBalancers"]
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("LoadBalancerNotFound", "LoadBalancerNotFoundException"):
                raise
        if not found:
            profile = self.lb_profile(rec) or "default"
            raise R53WError(f"load balancer '{ref}' nao encontrado (regiao {region}, profile {profile})")

        f = found[0]
        lb = LoadBalancer(
            dns=f["DNSName"],
            zone_id=f["CanonicalHostedZoneId"],
            kind=f.get("Type", "application"),
            scheme=f.get("Scheme", "?"),
            state=f.get("State", {}).get("Code", "?"),
            ipv6=f.get("IpAddressType", "ipv4") != "ipv4",
        )
        self._lbs[cache_key] = lb
        return lb

    def resolve_zone(self, rec: Record) -> str:
        if rec.zone_id:
            return rec.zone_id.split("/")[-1]
        name = fqdn(rec.zone)
        zones = self.r53(rec).list_hosted_zones_by_name(DNSName=name)["HostedZones"]
        matches = [z for z in zones if fqdn(z["Name"]) == name]
        if not matches:
            profile = self.dns_profile(rec) or "default"
            raise R53WError(
                f"hosted zone '{name}' nao encontrada com o profile '{profile}'. "
                "Se a zona fica em outra conta, defina 'dns_profile' no config ou use --dns-profile."
            )
        if len(matches) > 1:
            ids = ", ".join(
                f"{z['Id'].split('/')[-1]} ({'privada' if z['Config'].get('PrivateZone') else 'publica'})"
                for z in matches
            )
            raise R53WError(f"mais de uma hosted zone '{name}': {ids}. Defina 'zone_id' no config.")
        return matches[0]["Id"].split("/")[-1]

    def read_rrsets(self, zone_id: str, rec: Record) -> list[dict]:
        """Todos os record sets (de qualquer tipo) com o nome do registro."""
        out = []
        pages = self.r53(rec).get_paginator("list_resource_record_sets").paginate(
            HostedZoneId=zone_id, StartRecordName=rec.name
        )
        for page in pages:
            for rr in page["ResourceRecordSets"]:
                if fqdn(rr["Name"]) != rec.name:
                    return out
                out.append(rr)
        return out

    def apply(self, rec: Record, zone_id: str, changes: list[dict], comment: str) -> None:
        r53 = self.r53(rec)
        resp = r53.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch={"Comment": comment, "Changes": changes}
        )
        print("  aplicado, aguardando propagacao (INSYNC)...", flush=True)
        r53.get_waiter("resource_record_sets_changed").wait(
            Id=resp["ChangeInfo"]["Id"], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
        )
        print("  INSYNC")


# --------------------------------------------------------------------------
# Estado e diff
# --------------------------------------------------------------------------

@dataclass
class State:
    zone_id: str
    lbs: dict[str, LoadBalancer]          # alvo do config -> LB resolvido
    types: list[str]                      # tipos gerenciados; o primeiro e o principal
    by_type: dict[str, dict[str, dict]]   # tipo -> SetIdentifier -> rrset ponderado atual
    simple: list[dict]                    # registros simples no nome (A/AAAA/CNAME)
    other_policy: list[dict]              # mesmo nome com outra politica (latency, failover...)

    @property
    def weighted(self) -> dict[str, dict]:
        return self.by_type[self.types[0]]

    def weights(self, rec: Record) -> dict[str, int]:
        """Pesos atuais dos alvos do config (0 se o registro ainda nao existe)."""
        return {sid: self.weighted.get(sid, {}).get("Weight", 0) for sid in rec.targets}

    def foreign(self, rec: Record) -> dict[str, int]:
        """Registros ponderados que existem na Route53 mas nao estao no config."""
        return {sid: rr["Weight"] for sid, rr in self.weighted.items() if sid not in rec.targets}


def load_state(aws: AWS, rec: Record) -> State:
    zone_id = aws.resolve_zone(rec)
    rrsets = aws.read_rrsets(zone_id, rec)
    lbs = {sid: aws.resolve_lb(rec, t) for sid, t in rec.targets.items()}

    # Um registro A anda junto com o AAAA do mesmo nome: se todos os alvos atendem IPv6,
    # o AAAA recebe os mesmos pesos; senao ele e removido na conversao.
    family = {"A", "AAAA"} if rec.type == "A" else {rec.type}
    types = [rec.type]
    if rec.type == "A" and all(lb.ipv6 for lb in lbs.values()):
        types.append("AAAA")

    related = [rr for rr in rrsets if rr["Type"] in family | {"CNAME"}]
    weighted = [rr for rr in related if rr["Type"] in family and "Weight" in rr]
    return State(
        zone_id=zone_id,
        lbs=lbs,
        types=types,
        by_type={t: {rr["SetIdentifier"]: rr for rr in weighted if rr["Type"] == t} for t in sorted(family)},
        simple=[rr for rr in related if "SetIdentifier" not in rr],
        other_policy=[rr for rr in related if "SetIdentifier" in rr and "Weight" not in rr],
    )


def require_weighted(rec: Record, state: State, allow_simple: bool = False) -> None:
    """Garante que da para gravar registros ponderados neste nome."""
    if state.other_policy:
        rr = state.other_policy[0]
        raise R53WError(
            f"{rec.name} ja tem registro {rr['Type']} com outra politica de roteamento "
            f"(SetIdentifier '{rr['SetIdentifier']}'). Remova-o antes."
        )
    if state.simple and not allow_simple:
        rr = state.simple[0]
        example = " ".join(f"{sid}={100 if i == 0 else 0}" for i, sid in enumerate(rec.targets))
        raise R53WError(
            f"{rec.name} hoje e um registro simples ({rr['Type']} -> {rr_target(rr)}). "
            f"Para converter em ponderado sem intervalo: set {rec.key} {example} --replace-simple"
        )


def build_changes(rec: Record, state: State, new_weights: dict[str, int], rtype: str) -> tuple[list[dict], list[str]]:
    """UPSERTs do tipo `rtype` para chegar em `new_weights` + avisos sobre troca de alvo."""
    changes, notes = [], []
    for sid, weight in new_weights.items():
        lb = state.lbs[sid]
        current = state.by_type[rtype].get(sid)
        desired = {
            "Name": rec.name,
            "Type": rtype,
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
                notes.append(f"{sid}: alvo muda de {rr_target(current)} para {alias_key(lb.dns)}")
            elif not same_eth:
                notes.append(f"{sid}: EvaluateTargetHealth passa a ser {rec.evaluate_target_health}")
            if current["Weight"] == weight and same_target and same_eth:
                continue
        else:
            notes.append(f"{sid}: registro sera criado apontando para {alias_key(lb.dns)}")
        changes.append({"Action": "UPSERT", "ResourceRecordSet": desired})
    return changes, notes


def all_changes(rec: Record, state: State, new_weights: dict[str, int]) -> tuple[list[dict], list[str]]:
    """Mudancas no tipo principal e, se gerenciado, no AAAA espelhando os mesmos pesos."""
    changes, notes = build_changes(rec, state, new_weights, state.types[0])
    final = {**{sid: w for sid, w in state.weights(rec).items() if sid in state.weighted}, **new_weights}
    for rtype in state.types[1:]:
        extra, _ = build_changes(rec, state, final, rtype)
        if extra:
            notes.append(f"{rtype}: {len(extra)} registro(s) ponderado(s) acompanham os mesmos pesos")
        changes += extra
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
# Check: testa cada ALB direto, sem depender do DNS do registro
# --------------------------------------------------------------------------

def http_probe(ip: str, hostname: str, check: Check) -> tuple[bool, str]:
    """GET em `ip` como se fosse `hostname` (SNI + Host). Valida certificado e status."""
    port = check.port or (443 if check.scheme == "https" else 80)
    conn = http.client.HTTPConnection(ip, port, timeout=check.timeout)
    try:
        sock = socket.create_connection((ip, port), timeout=check.timeout)
        if check.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=hostname)
        conn.sock = sock
        conn.request("GET", check.path, headers={"Host": hostname, "User-Agent": "r53w-check"})
        status = conn.getresponse().status
    except ssl.SSLCertVerificationError as e:
        return False, f"certificado nao vale para {hostname}: {e.verify_message}"
    except ssl.SSLError as e:
        return False, f"erro de TLS: {e.reason or e}"
    except (TimeoutError, socket.timeout):
        return False, f"sem resposta em {check.timeout:g}s (ALB interno exige VPN; ou security group)"
    except (OSError, http.client.HTTPException) as e:
        return False, f"falha de conexao: {e}"
    finally:
        conn.close()
    ok = status in check.expect if check.expect else 200 <= status < 400
    return ok, f"HTTP {status}" + ("" if ok else " (inesperado)")


def run_check(rec: Record, state: State, sids: list[str]) -> bool:
    """Testa os alvos `sids`. Imprime o resultado e retorna True se tudo passou."""
    check = rec.check
    hostname = check.host or rec.name.rstrip(".")
    if "*" in hostname:
        raise R53WError(f"{rec.key}: registro wildcard; defina 'check.host' com um nome real para testar")
    port = check.port or (443 if check.scheme == "https" else 80)
    print(f"  check: GET {check.scheme}://{hostname}{check.path} direto em cada load balancer")

    ok_all = True
    schemes = {state.lbs[sid].scheme for sid in rec.targets} - {"?"}
    if len(schemes) > 1:
        detail = ", ".join(f"{sid}={state.lbs[sid].scheme}" for sid in rec.targets)
        print(f"    FALHA  os load balancers tem schemes diferentes ({detail}): parte dos clientes nao vai conectar")
        ok_all = False

    for sid in sids:
        lb = state.lbs[sid]
        if lb.state != "active":
            print(f"    FALHA  {sid}: load balancer em estado '{lb.state}'")
            ok_all = False
            continue
        try:
            infos = socket.getaddrinfo(lb.dns, port, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror as e:
            print(f"    FALHA  {sid}: DNS do load balancer nao resolve ({lb.dns}): {e}")
            ok_all = False
            continue
        for ip in sorted({info[4][0] for info in infos}):
            ok, detail = http_probe(ip, hostname, check)
            print(f"    {'ok   ' if ok else 'FALHA'}  {sid} ({lb.scheme}) {ip}: {detail}")
            ok_all = ok_all and ok
    return ok_all


def precheck(rec: Record, state: State, sids: list[str], skip: bool) -> None:
    """Check obrigatorio antes de dar trafego a um alvo. Aborta se falhar."""
    if skip or not sids:
        return
    if not run_check(rec, state, sids):
        raise R53WError("check falhou; nada foi alterado. Corrija, ou use --skip-check para ignorar.")


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------

def cmd_show(aws: AWS, records: dict[str, Record], args) -> int:
    keys = [args.record] if args.record else list(records)
    for key in keys:
        rec = get_record(records, key)
        state = load_state(aws, rec)
        print(f"{rec.key}: {rec.name} {rec.type}  (zona {state.zone_id})")
        for rr in state.simple:
            print(f"  registro SIMPLES (sem peso): {rr['Type']} -> {rr_target(rr)}")
        for rr in state.other_policy:
            print(f"  registro com OUTRA politica: {rr['Type']} '{rr['SetIdentifier']}' -> {rr_target(rr)}")
        weights = {**state.weights(rec), **state.foreign(rec)}
        total = sum(weights.values())
        width = max(len(s) for s in weights)
        for sid, w in weights.items():
            rr = state.weighted.get(sid)
            if rr is None:
                detail = f"AUSENTE na Route53 (config: {alias_key(state.lbs[sid].dns)})"
            else:
                alias = rr.get("AliasTarget")
                detail = rr_target(rr) if alias else f"registro nao-alias ({rr_target(rr)})"
                if alias and not alias["EvaluateTargetHealth"]:
                    detail += "  [health desligado]"
                if sid not in rec.targets:
                    detail += "  (fora do config)"
                elif not alias or rr_target(rr) != alias_key(state.lbs[sid].dns):
                    detail += f"  [DIFERE do config: {alias_key(state.lbs[sid].dns)}]"
            print(f"  {sid:<{width}}  {w:>3}  {pct(w, total)}  {detail}")
        if state.simple:
            example = " ".join(f"{sid}={100 if i == 0 else 0}" for i, sid in enumerate(rec.targets))
            print(f"  para converter em ponderado: set {rec.key} {example} --replace-simple")
        elif state.weighted and total == 0:
            warn("todos os pesos sao 0: a Route53 distribui igualmente entre todos os registros")
        primary = {sid: rr["Weight"] for sid, rr in state.weighted.items()}
        for rtype in state.types[1:]:
            if primary and {sid: rr["Weight"] for sid, rr in state.by_type[rtype].items()} != primary:
                warn(f"os registros {rtype} nao espelham os pesos do {state.types[0]}; rode um 'set' para alinhar")
        print()
    return 0


def cmd_check(aws: AWS, records: dict[str, Record], args) -> int:
    rec = get_record(records, args.record)
    for sid in args.targets:
        if sid not in rec.targets:
            raise R53WError(f"alvo '{sid}' nao existe em '{rec.key}' (alvos: {', '.join(rec.targets)})")
    state = load_state(aws, rec)
    print(f"{rec.key}: {rec.name} {rec.type}")
    ok = run_check(rec, state, args.targets or list(rec.targets))
    print("  resultado: " + ("todos os alvos responderam como esperado" if ok else "FALHOU"))
    return 0 if ok else 1


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
    require_weighted(rec, state, allow_simple=args.replace_simple)

    print(f"{rec.key}: {rec.name} {rec.type}")
    deletes, notes = [], []
    if state.simple:
        missing = [sid for sid in rec.targets if sid not in new_weights and sid not in state.weighted]
        if missing:
            raise R53WError(f"na conversao de registro simples, informe o peso de todos os alvos (faltou: {', '.join(missing)})")
        for rr in state.simple:
            print(f"  REMOVER registro simples: {rr['Type']} -> {rr_target(rr)}")
            deletes.append({"Action": "DELETE", "ResourceRecordSet": rr})
            if rr["Type"] == "AAAA" and "AAAA" not in state.types:
                notes.append("o AAAA simples sai sem substituto: nem todos os alvos atendem IPv6")
        # A conversao so e "invisivel" se 100% do peso ficar no LB que ja recebe o trafego hoje.
        main = next((rr for rr in state.simple if rr["Type"] != "AAAA"), None)
        if main:
            current_target = rr_target(main)
            owner = next((sid for sid, lb in state.lbs.items() if alias_key(lb.dns) == current_target), None)
            if owner is None:
                notes.append(f"o registro atual aponta para {current_target}, que nao e nenhum dos alvos do config")
            elif sum(new_weights.values()) != new_weights[owner]:
                notes.append(f"hoje 100% do trafego vai para '{owner}'; esta conversao ja muda a distribuicao")

    upserts, more_notes = all_changes(rec, state, new_weights)
    changes = deletes + upserts
    print_diff(rec, state, new_weights)
    for note in notes + more_notes:
        warn(note)
    final = {**state.weights(rec), **state.foreign(rec), **new_weights}
    if sum(final.values()) == 0:
        warn("todos os pesos ficam 0: a Route53 distribui igualmente entre todos os registros")
    if state.foreign(rec):
        warn(f"existem registros fora do config que tambem recebem trafego: {fmt_weights(state.foreign(rec))}")

    if not changes:
        print("  nada a alterar.")
        return 0
    before = state.weights(rec)
    gaining = [sid for sid, w in new_weights.items() if w > before[sid]]
    if not args.apply:
        if gaining and not args.skip_check:
            print(f"  com --apply, o check roda antes em: {', '.join(gaining)} (ou rode agora: check {rec.key})")
        print("  dry-run: nada foi alterado. Repita com --apply para aplicar.")
        return 0
    precheck(rec, state, gaining, args.skip_check)
    aws.apply(rec, state.zone_id, changes, f"r53w set {rec.key} {fmt_weights(new_weights)}")
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
    require_weighted(rec, state)
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
        if not args.skip_check:
            print(f"  com --apply, o check roda antes em: {args.to} (ou rode agora: check {rec.key})")
        print("  dry-run: nada foi alterado. Repita com --apply para aplicar.")
        return 0
    precheck(rec, state, [args.to], args.skip_check)

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
            changes, notes = all_changes(rec, state, weights)
            for note in notes:
                warn(note)
            if changes:
                aws.apply(rec, state.zone_id, changes, f"r53w shift {rec.key} -> {args.to} ({i}/{len(plan)})")
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


def cmd_simplify(aws: AWS, records: dict[str, Record], args) -> int:
    """Caminho de volta: troca os registros ponderados por um simples, num unico lote."""
    rec = get_record(records, args.record)
    if args.to not in rec.targets:
        raise R53WError(f"alvo '{args.to}' nao existe em '{rec.key}' (alvos: {', '.join(rec.targets)})")
    state = load_state(aws, rec)
    require_weighted(rec, state, allow_simple=True)

    print(f"{rec.key}: {rec.name} {rec.type}")
    weighted = [rr for by_sid in state.by_type.values() for rr in by_sid.values()]
    if not weighted:
        print("  nao ha registros ponderados neste nome. Nada a fazer.")
        return 0

    lb = state.lbs[args.to]
    changes = []
    for rr in weighted:
        print(f"  REMOVER ponderado: {rr['Type']} '{rr['SetIdentifier']}' peso {rr['Weight']} -> {rr_target(rr)}")
        changes.append({"Action": "DELETE", "ResourceRecordSet": rr})
    for rtype in [rec.type] + (["AAAA"] if rec.type == "A" and lb.ipv6 else []):
        print(f"  CRIAR simples:     {rtype} -> {alias_key(lb.dns)}")
        changes.append({"Action": "UPSERT", "ResourceRecordSet": {
            "Name": rec.name,
            "Type": rtype,
            "AliasTarget": {
                "HostedZoneId": lb.zone_id,
                "DNSName": lb.alias_dns,
                "EvaluateTargetHealth": rec.evaluate_target_health,
            },
        }})

    current = {**state.weights(rec), **state.foreign(rec)}
    total = sum(current.values())
    full = total > 0 and current[args.to] == total
    if not full:
        share = pct(current[args.to], total).strip() if total else "uma parte"
        warn(f"hoje '{args.to}' recebe {share} do trafego; depois desta troca recebera 100%")

    if not args.apply:
        print("  dry-run: nada foi alterado. Repita com --apply para aplicar.")
        return 0
    precheck(rec, state, [] if full else [args.to], args.skip_check)
    aws.apply(rec, state.zone_id, changes, f"r53w simplify {rec.key} -> {args.to}")
    print("  se o dominio vai voltar para o ExternalDNS, remova agora a anotacao 'controller' dos Ingress.")
    return 0


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
    p.add_argument("--profile", help="profile da AWS da conta dos load balancers (sobrepoe o config)")
    p.add_argument("--dns-profile", help="profile da AWS da conta da hosted zone (sobrepoe o config)")
    p.add_argument("--region", help="regiao dos load balancers, quando nao esta no config")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("show", help="mostra os pesos atuais")
    s.add_argument("record", nargs="?", help="registro do config (default: todos)")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("check", help="testa cada ALB direto, sem passar pelo DNS do registro")
    s.add_argument("record")
    s.add_argument("targets", nargs="*", metavar="alvo", help="alvos a testar (default: todos)")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("set", help="define pesos (alvos omitidos nao mudam)")
    s.add_argument("record")
    s.add_argument("weights", nargs="+", metavar="alvo=peso", help=f"peso relativo de 0 a {MAX_WEIGHT}")
    s.add_argument("--replace-simple", action="store_true",
                   help="troca o registro simples existente pelos ponderados, numa unica operacao")
    s.add_argument("--skip-check", action="store_true", help="nao testa os ALBs antes de aplicar")
    s.add_argument("--apply", action="store_true", help="aplica de verdade (sem isso e dry-run)")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("shift", help="migra trafego gradualmente para um alvo")
    s.add_argument("record")
    s.add_argument("--to", required=True, help="alvo que vai receber o trafego")
    s.add_argument("--step", type=int, default=10, help="pontos percentuais por passo (default: 10)")
    s.add_argument("--until", type=int, default=100, help="percentual final do alvo (default: 100)")
    s.add_argument("--interval", type=int, default=60, help="segundos entre passos (default: 60)")
    s.add_argument("--skip-check", action="store_true", help="nao testa o ALB de destino antes de aplicar")
    s.add_argument("--apply", action="store_true", help="aplica de verdade (sem isso e dry-run)")
    s.set_defaults(func=cmd_shift)

    s = sub.add_parser("simplify", help="troca os registros ponderados por um simples (caminho de volta)")
    s.add_argument("record")
    s.add_argument("--to", required=True, help="alvo para onde o registro simples vai apontar")
    s.add_argument("--skip-check", action="store_true", help="nao testa o alvo antes de aplicar")
    s.add_argument("--apply", action="store_true", help="aplica de verdade (sem isso e dry-run)")
    s.set_defaults(func=cmd_simplify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = load_config(args.config)
        aws = AWS(args.profile, args.dns_profile, args.region)
        return args.func(aws, records, args)
    except R53WError as e:
        print(f"erro: {e}", file=sys.stderr)
    except (ClientError, BotoCoreError) as e:
        print(f"erro da AWS: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
