param(
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

# Keep this bootstrap script ASCII-only. Windows PowerShell 5.1 reads a UTF-8
# file without BOM by using the system ANSI code page and can corrupt CJK text.
# Existing LLM URL/model/API key values are preserved and never printed.
$target = Join-Path (Get-Location) $EnvFile
if (-not (Test-Path -LiteralPath $target)) {
    Copy-Item -LiteralPath ".env.example" -Destination $target
}

$updates = [ordered]@{
    AIRLINE_MVP_DATABASE_BACKEND = "postgres"
    AIRLINE_MVP_DATABASE_URL = "postgresql://airline_mvp:airline_mvp_dev@127.0.0.1:5432/airline_mvp"
    AIRLINE_MVP_CHECKPOINT_BACKEND = "postgres"
    AIRLINE_MVP_CHECKPOINT_DATABASE_URL = ""
    AIRLINE_MVP_KNOWLEDGE_BACKEND = "postgres"
    AIRLINE_MVP_EMBEDDING_BACKEND = "local_fastembed"
    AIRLINE_MVP_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
    AIRLINE_MVP_EMBEDDING_DIMENSIONS = "512"
}

$lines = [System.Collections.Generic.List[string]]::new()
Get-Content -LiteralPath $target -Encoding UTF8 | ForEach-Object {
    $lines.Add($_)
}

foreach ($entry in $updates.GetEnumerator()) {
    $prefix = $entry.Key + "="
    $replacement = $prefix + $entry.Value
    $index = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].StartsWith($prefix, [StringComparison]::Ordinal)) {
            $index = $i
            break
        }
    }
    if ($index -ge 0) {
        $lines[$index] = $replacement
    } else {
        $lines.Add($replacement)
    }
}

[System.IO.File]::WriteAllLines($target, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Host "PostgreSQL/pgvector/FastEmbed settings were written to $target. Existing LLM secrets were preserved and were not printed."
