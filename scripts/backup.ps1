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

# Criptografia opcional (BACKUP_PASSPHRASE no .env ou no ambiente): o dump leva
# o COFRE da familia em claro e esta pasta pode sincronizar no OneDrive.
# Restaurar: gpg -d arquivo.sql.gpg | docker exec -i mordomo-postgres psql -U mordomo -d mordomo
$pass = $env:BACKUP_PASSPHRASE
if (-not $pass -and (Test-Path .env)) {
    $linha = Select-String -Path .env -Pattern '^BACKUP_PASSPHRASE=(.+)$' | Select-Object -Last 1
    if ($linha) { $pass = $linha.Matches[0].Groups[1].Value }
}
if ($pass) {
    if (Get-Command gpg -ErrorAction SilentlyContinue) {
        gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase $pass -o "$arquivo.gpg" $arquivo
        if ($LASTEXITCODE -ne 0) { throw "gpg falhou (exit $LASTEXITCODE)" }
        Remove-Item $arquivo -Force
        $arquivo = "$arquivo.gpg"
    } else {
        Write-Warning "BACKUP_PASSPHRASE definida mas gpg nao encontrado (instale Gpg4win); dump ficou em texto puro"
    }
} else {
    Write-Warning "Sem BACKUP_PASSPHRASE: dump em TEXTO PURO (contem o cofre, e a pasta pode sincronizar no OneDrive)"
}

$tamanho = [math]::Round((Get-Item $arquivo).Length / 1KB, 1)
Write-Host "Backup gravado: $arquivo ($tamanho KB)"

# Retencao: apaga dumps mais velhos que -ManterDias (texto puro e .gpg)
$limite = (Get-Date).AddDays(-$ManterDias)
$antigos = Get-ChildItem $Destino -Filter "mordomo_*.sql*" | Where-Object { $_.LastWriteTime -lt $limite }
foreach ($a in $antigos) {
    Remove-Item $a.FullName -Force
    Write-Host "  removido (mais de $ManterDias dias): $($a.Name)"
}
