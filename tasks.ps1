# Equivalente ao Makefile para Windows (make não vem no Windows).
#
#   .\tasks.ps1 install     dependências (uv sync)
#   .\tasks.ps1 up          sobe o Postgres
#   .\tasks.ps1 down        derruba o Postgres
#   .\tasks.ps1 db-init     cria as tabelas
#   .\tasks.ps1 seed        cadastra a família de exemplo
#   .\tasks.ps1 run         inicia o mordomo
#   .\tasks.ps1 test        testes (sem rede, sem chaves)
#   .\tasks.ps1 evals       eval de datas pt-BR (--com-llm inclui roteamento)
#   .\tasks.ps1 lint        ruff
#
# Argumentos extras são repassados: .\tasks.ps1 evals --com-llm
#
# "A execução de scripts foi desabilitada neste sistema"? Libere uma vez:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# ou chame: powershell -ExecutionPolicy Bypass -File .\tasks.ps1 <alvo>

param(
    [Parameter(Position = 0)]
    [string]$Alvo = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Extras = @()
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

switch ($Alvo) {
    "install" { uv sync --extra whatsapp @Extras }
    "up"      { docker compose up -d @Extras }
    "down"    { docker compose down @Extras }
    "db-init" { uv run python scripts/init_db.py @Extras }
    "seed"    { uv run python scripts/seed_familia.py --exemplo @Extras }
    "run"     { uv run python -m mordomo.main @Extras }
    "test"    { uv run pytest -q @Extras }
    "evals"   { uv run python evals/run_evals.py @Extras }
    "lint"    { uv run ruff check src tests @Extras }
    "replay"  { uv run python scripts/replay_whatsapp.py --exemplo @Extras }
    default {
        Write-Host "Alvos: install, up, down, db-init, seed, run, test, evals, lint, replay"
        Write-Host "Exemplo: .\tasks.ps1 evals --com-llm"
    }
}

if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { exit $LASTEXITCODE }
