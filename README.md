# r53w

CLI para gerenciar pesos (weighted routing) de registros alias na Route53 apontando para ALBs.

## Pré-requisitos

- Python 3.10+
- Credenciais da AWS configuradas (`aws configure`, SSO ou variáveis de ambiente)
- Permissões IAM:
  - `route53:ListHostedZonesByName`
  - `route53:ListResourceRecordSets`
  - `route53:ChangeResourceRecordSets`
  - `route53:GetChange`
  - `elasticloadbalancing:DescribeLoadBalancers`

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
region: us-east-1             # região padrão dos ALBs

records:
  api:                        # apelido usado na linha de comando
    zone: exemplo.com.        # nome da hosted zone (ou zone_id: Z123...)
    name: api.exemplo.com.
    targets:                  # apelido -> ALB (nome, ARN ou DNS)
      blue: meu-alb-blue
      green: meu-alb-green
```

Opções por registro:

| Campo | Default | Descrição |
|---|---|---|
| `zone` / `zone_id` | obrigatório | Use `zone_id` se houver zona pública e privada com o mesmo nome |
| `name` | obrigatório | Nome do registro DNS |
| `type` | `A` | Tipo do registro |
| `region` | o do topo | Região dos ALBs deste registro |
| `evaluate_target_health` | `true` | Route53 tira do ar o ALB que estiver unhealthy |

Para um ALB em outra região, use a forma longa no alvo:

```yaml
    targets:
      primario: alb-a
      secundario:
        lb: alb-b
        region: sa-east-1
```

## Uso

Nada é alterado sem `--apply`: sem essa flag, `set` e `shift` só mostram o que fariam.

### Ver os pesos atuais

```bash
uv run r53w.py show          # todos os registros do config
uv run r53w.py show api
```

### Definir pesos

```bash
uv run r53w.py set api blue=90 green=10           # dry-run
uv run r53w.py set api blue=90 green=10 --apply
```

- Os pesos vão de 0 a 255 e são relativos: `90/10` e `9/1` dão o mesmo resultado.
- Alvo omitido mantém o peso atual.
- Se os registros ainda não existem, o primeiro `set --apply` cria.

### Migrar tráfego gradualmente

```bash
uv run r53w.py shift api --to green --step 10 --interval 60           # mostra o plano
uv run r53w.py shift api --to green --step 10 --interval 60 --apply
uv run r53w.py shift api --to green --until 50 --apply                # para em 50%
```

| Flag | Default | Descrição |
|---|---|---|
| `--to` | obrigatório | Alvo que vai receber o tráfego |
| `--step` | `10` | Pontos percentuais por passo |
| `--until` | `100` | Percentual final do alvo |
| `--interval` | `60` | Segundos entre os passos |

A cada passo o script aplica os pesos, espera a Route53 propagar (`INSYNC`) e aguarda o intervalo.
Se for interrompido (Ctrl+C, erro, ou alguém alterar os pesos por fora), ele para e imprime o comando para voltar ao estado inicial.

### Opções globais

Vêm antes do subcomando:

```bash
uv run r53w.py --profile producao --region us-east-1 -c outro.yaml show
```

| Flag | Descrição |
|---|---|
| `-c`, `--config` | Arquivo de config (default: `records.yaml`) |
| `--profile` | Profile da AWS |
| `--region` | Região dos ALBs, quando não está no config |

## Limitações

- Se o nome já existe como registro simples (não ponderado), o script para com erro: remova ou converta esse registro antes.
- O `shift` se recusa a rodar se houver registros ponderados com peso fora do config.
- O `shift` não verifica a saúde dos targets do ALB entre os passos; quem protege é o `evaluate_target_health` da Route53.
