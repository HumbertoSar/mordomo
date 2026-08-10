# Backup do Postgres do Mordomo (domínio + eventos + checkpoints do LangGraph).
#
#   .\scripts\backup.ps1                      # grava em .\backups
#   .\scripts\backup.ps1 -Destino D:\backups  # outro lugar
#
# Dado de família não tem segunda chance: aqui moram os lembretes, a agenda, o
# histórico de conversa (checkpointer) e a série inteira de product_events, que
# é a matéria-prima do dashboard e dos evals. Um `docker compose down -v`
# distraído apaga tudo isso.
#
# Para agendar diariamente às 3h (uma vez só, num PowerShell de administrador):
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#        -Argument "-NoProfile -File `"$PWD\scripts\backup.ps1`""
#   $g = New-ScheduledTaskTrigger -Daily -At 3am
#   Register-ScheduledTask -TaskName "Mordomo backup" -Action $a -Trigger $g

param(
    [string]$Destino = "backups",
    [string]$Container = "mordomo-postgres",
    [int]$ManterDias = 30
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path $Destino)) { New-Item -ItemType Directory -Force $Destino | Out-Null }

$estado = docker inspect -f '{{.State.Running}}' $Container 2>$null
if ($estado -ne "true") { throw "Container '$Container' nao esta rodando. Suba com: docker compose up -d" }

$carimbo = Get-Date -Format "yyyy-MM-dd_HHmm"
$arquivo = Join-Path $Destino "mordomo_$carimbo.sql"

# -Fc daria formato comprimido, mas o texto puro permite inspecionar e fazer
# diff do dump — util num projeto cujo objetivo e ENTENDER os proprios dados.
docker exec $Container pg_dump -U mordomo -d mordomo | Out-File -FilePath $arquivo -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "pg_dump falhou (exit $LASTEXITCODE)" }

$tamanho = [math]::Round((Get-Item $arquivo).Length / 1KB, 1)
Write-Host "Backup gravado: $arquivo ($tamanho KB)"

# Retencao: apaga dumps mais velhos que -ManterDias
$limite = (Get-Date).AddDays(-$ManterDias)
$antigos = Get-ChildItem $Destino -Filter "mordomo_*.sql" | Where-Object { $_.LastWriteTime -lt $limite }
foreach ($a in $antigos) {
    Remove-Item $a.FullName -Force
    Write-Host "  removido (mais de $ManterDias dias): $($a.Name)"
}
