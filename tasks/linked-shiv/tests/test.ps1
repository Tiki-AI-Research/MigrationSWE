param([Parameter(Mandatory=$true)][string]$CandidateRoot, [string]$Output = "verifier-result.json", [string]$Docker = "docker")
$Python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
& $Python "$PSScriptRoot\verifier\verifier.py" --candidate-root $CandidateRoot --config "$PSScriptRoot\config.json" --output $Output --docker $Docker
exit $LASTEXITCODE
