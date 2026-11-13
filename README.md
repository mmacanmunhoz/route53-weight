# r53w

CLI para gerenciar pesos (weighted routing) de registros alias na Route53 apontando para load balancers (ALB ou NLB).

O script altera a Route53 diretamente. Em clusters com ExternalDNS, o domínio precisa ser
tirado do controle dele antes (veja [Uso com ExternalDNS](#uso-com-externaldns)).

## Comandos

| Comando | O que faz |
|---|---|
| `show` | Mostra os registros e pesos atuais |
| `check` | Testa cada load balancer direto, sem passar pelo DNS |
| `set` | Define os pesos; com `--replace-simple`, converte um registro simples em ponderado |
| `shift` | Migra o tráfego aos poucos para um alvo |
| `simplify` | Volta de ponderado para registro simples |

`set`, `shift` e `simplify` só alteram alguma coisa com `--apply`.

## Pré-requisitos

- Python 3.10+
- Credenciais da AWS configuradas (`aws sso login`, `aws configure` ou variáveis de ambiente)
- Permissões IAM na conta da hosted zone:
  - `route53:ListHostedZonesByName`
  - `route53:ListResourceRecordSets`
  - `route53:ChangeResourceRecordSets`
  - `route53:GetChange`
- Permissão IAM na conta dos load balancers (dispensável no modo direto):
  - `elasticloadbalancing:DescribeLoadBalancers`
- Para o `check` de load balancers internos: acesso de rede a eles (VPN)

## Instalação

Com [uv](https://docs.astral.sh/uv/) não precisa instalar nada, ele resolve as dependências sozinho:

```bash
uv run r53w.py --help
```

Ou com pip:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python r53w.py --help
```

Os exemplos abaixo usam `uv run r53w.py`; com pip, troque por `python r53w.py`.

## Configuração

Copie o modelo e ajuste:

```bash
cp records.example.yaml records.yaml
```

```yaml
region: us-east-1               # região padrão dos load balancers
profile: conta-apps             # conta onde ficam os load balancers
dns_profile: conta-dns          # conta onde fica a hosted zone (omita se for a mesma)

check:
  path: /health

records:
  api:                          # apelido usado na linha de comando
    zone: exemplo.com.          # nome da hosted zone (ou zone_id: Z123...)
    name: api.exemplo.com.
    targets:                    # apelido -> load balancer (nome, ARN ou DNS)
      antigo: k8s-app-antigo-0123456789-111111111.us-east-1.elb.amazonaws.com
      novo: k8s-app-novo-0123456789-222222222.us-east-1.elb.amazonaws.com
```

Para load balancers criados pelo cluster, o mais simples é usar o DNS que aparece em
`kubectl get ingress` (coluna ADDRESS).

Cada alvo aceita três formas:

```yaml
    targets:
      a: meu-alb                      # nome, ARN ou DNS: o script consulta a API do ELB
      b:
        lb: outro-alb                 # idem, com região própria
        region: sa-east-1
      c:                              # modo direto: não consulta o ELB
        dns: k8s-app-0123456789-111111111.us-east-1.elb.amazonaws.com
        hosted_zone_id: Z35SXDOTRQ7X7K
        ipv6: false                   # default
```

O modo direto serve para quem não tem permissão de leitura no ELB. O `hosted_zone_id` é o do
load balancer, não o da sua zona: em `us-east-1`, `Z35SXDOTRQ7X7K` para ALB e `Z26RNL4JYFTOTI`
para NLB. Nesse modo o script não confere se o load balancer está ativo nem se é interno ou público.

Opções por registro (as três primeiras do topo também valem como padrão para todos):

| Campo | Default | Descrição |
|---|---|---|
| `region` | região da sessão | Região dos load balancers |
| `profile` | profile padrão | Profile da AWS da conta dos load balancers |
| `dns_profile` | igual a `profile` | Profile da AWS da conta da hosted zone |
| `zone` / `zone_id` | obrigatório | Use `zone_id` se houver zona pública e privada com o mesmo nome |
| `name` | obrigatório | Nome do registro DNS |
| `type` | `A` | Tipo do registro |
| `evaluate_target_health` | `true` | Route53 deixa de responder com o load balancer que não tiver target saudável |
| `check.path` | `/` | Caminho usado no teste dos load balancers |
| `check.scheme` | `https` | `http` ou `https` |
| `check.port` | 443 / 80 | Porta do teste |
| `check.expect` | qualquer 2xx/3xx | Lista de status aceitos, ex.: `[200, 401]` |
| `check.host` | o `name` | Host usado no teste (obrigatório para registro wildcard) |
| `check.timeout` | `5` | Segundos |

## Uso

Nada é alterado sem `--apply`: sem essa flag, `set`, `shift` e `simplify` só mostram o que fariam.

### Ver o estado atual

```bash
uv run r53w.py show          # todos os registros do config
uv run r53w.py show api
```

### Testar os load balancers

```bash
uv run r53w.py check api          # todos os alvos
uv run r53w.py check api novo     # só um
```

Faz uma requisição direto em cada IP de cada load balancer, como se fosse o domínio (SNI e
`Host`), sem passar pelo DNS do registro. Pega: certificado que não vale para o domínio, regra
de host faltando (404), security group fechado, load balancer que não está ativo e mistura de
interno com público.

Esse mesmo teste roda automaticamente antes de um `set` ou `shift` dar tráfego a um alvo;
se falhar, nada é alterado. `--skip-check` desliga.

### Definir pesos

```bash
uv run r53w.py set api antigo=90 novo=10           # dry-run
uv run r53w.py set api antigo=90 novo=10 --apply
```

- Os pesos vão de 0 a 255 e são relativos: `90/10` e `9/1` dão o mesmo resultado.
- Alvo omitido mantém o peso atual.

### Converter um registro simples em ponderado

Se o domínio hoje é um registro simples, a Route53 não deixa criar os ponderados ao lado dele.
O `--replace-simple` remove o simples e cria os ponderados na mesma operação, sem intervalo:

```bash
uv run r53w.py set api antigo=100 novo=0 --replace-simple           # confira o que será feito
uv run r53w.py set api antigo=100 novo=0 --replace-simple --apply
```

Dê 100 ao load balancer que já recebe o tráfego hoje: assim a resposta do DNS não muda para
ninguém. O script avisa se os pesos escolhidos já alteram a distribuição.

O registro AAAA (IPv6) do mesmo nome é tratado no mesmo lote:

- se todos os alvos atendem IPv6, são criados registros AAAA ponderados, que passam a
  acompanhar os mesmos pesos em todo `set` e `shift`;
- se algum não atende, o AAAA simples é removido (o script avisa).

### Migrar tráfego gradualmente

```bash
uv run r53w.py shift api --to novo --step 10 --interval 60           # mostra o plano
uv run r53w.py shift api --to novo --step 10 --interval 60 --apply
uv run r53w.py shift api --to novo --until 50 --apply                # para em 50%
```

| Flag | Default | Descrição |
|---|---|---|
| `--to` | obrigatório | Alvo que vai receber o tráfego |
| `--step` | `10` | Pontos percentuais por passo |
| `--until` | `100` | Percentual final do alvo |
| `--interval` | `60` | Segundos entre os passos |
| `--skip-check` | | Não testa o alvo de destino antes |

A cada passo o script aplica os pesos, espera a Route53 propagar (`INSYNC`) e aguarda o intervalo.
Se for interrompido (Ctrl+C, erro, ou alguém alterar os pesos por fora), ele para e imprime o
comando para voltar ao estado inicial.

### Voltar para registro simples

No fim da migração, ou para desistir dela, troca os ponderados por um registro simples
apontando para um alvo, também numa operação só:

```bash
uv run r53w.py simplify api --to novo           # confira o que será feito
uv run r53w.py simplify api --to novo --apply
```

O script avisa se o alvo escolhido não recebe 100% do tráfego hoje, e nesse caso testa o alvo
antes de aplicar.

### Opções globais

Vêm antes do subcomando e sobrepõem o config:

```bash
uv run r53w.py --profile conta-apps --dns-profile conta-dns -c outro.yaml show
```

| Flag | Descrição |
|---|---|
| `-c`, `--config` | Arquivo de config (default: `records.yaml`) |
| `--profile` | Profile da conta dos load balancers |
| `--dns-profile` | Profile da conta da hosted zone |
| `--region` | Região dos load balancers, quando não está no config |

## Uso com ExternalDNS

Se o ExternalDNS cria o registro a partir do Ingress, ele precisa parar de cuidar desse domínio,
senão os dois brigam pelo mesmo registro. Passo a passo:

1. **Tirar o Ingress do ExternalDNS.** Adicione nos dois Ingress, pelo Git/ArgoCD:

   ```yaml
   metadata:
     annotations:
       external-dns.alpha.kubernetes.io/controller: "none"
   ```

   Com essa anotação o ExternalDNS pula o Ingress inteiro, ou seja, **todos os hosts** dele.
   Com `--policy=upsert-only` os registros que já existem não são apagados, então nada cai.

2. **Testar os dois load balancers:** `r53w.py check api`. O de destino precisa atender o host
   real; se responder 404, ajuste o Ingress dele antes.

3. **Converter o registro**, mantendo 100% no load balancer atual:
   `r53w.py set api antigo=100 novo=0 --replace-simple --apply`

4. **Migrar aos poucos:** comece com `r53w.py set api antigo=95 novo=5 --apply`, observe os
   erros e depois siga com o `shift`.

5. **Devolver ao ExternalDNS** quando terminar: `r53w.py simplify api --to novo --apply` e depois
   remova a anotação dos Ingress. Deixe o host só no Ingress que deve continuar atendendo.

O registro TXT de dono criado pelo ExternalDNS continua na zona durante todo o processo; é ele
que permite ao ExternalDNS reassumir o registro no fim.

## Limitações

- Enquanto o domínio estiver com o script, o ExternalDNS não o atualiza. Se um load balancer for recriado,
  ajuste o config e rode o `set` de novo.
- O `simplify` só recria o AAAA se o alvo atende IPv6. O ExternalDNS cria o dele quando reassume.
- Voltar atrás não é instantâneo: o DNS fica em cache por cerca de 1 minuto (alguns clientes
  guardam por mais tempo).
- O `check` roda só antes de começar; o `shift` não reavalia a saúde entre os passos.
- O `shift` se recusa a rodar se houver registros ponderados com peso fora do config.
