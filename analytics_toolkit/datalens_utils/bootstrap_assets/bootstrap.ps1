# Install runtime tools outside the shareable project; PowerShell 5.1+.
param([Parameter(Mandatory=$true)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
Set-Location $ProjectRoot

$UvVersion = '0.12.23'
$YcVersion = '1.40.0'
$PythonVersion = (Get-Content '.python-version' -Raw).Trim()
if ($env:OS -ne 'Windows_NT' -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Use 64-bit PowerShell on Windows x86-64. Use bootstrap.sh on macOS/Linux.'
}
$ProjectName = (Split-Path $ProjectRoot -Leaf).Replace(' ', '')
$RuntimeRoot = if ($env:DATALENS_RUNTIME_DIR) { [IO.Path]::GetFullPath($env:DATALENS_RUNTIME_DIR) } else { [IO.Path]::GetFullPath((Join-Path (Split-Path $ProjectRoot -Parent) ".local/$ProjectName")) }
if ($RuntimeRoot -eq $ProjectRoot -or $RuntimeRoot.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw 'DATALENS_RUNTIME_DIR must be outside the project.'
}
$env:DATALENS_RUNTIME_DIR = $RuntimeRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
$ToolDir = Join-Path $RuntimeRoot '.tools/windows-amd64'
$BinDir = "$ToolDir/bin"
New-Item -ItemType Directory -Force $BinDir, (Join-Path $RuntimeRoot '.cache/uv') | Out-Null
$Stage = Join-Path ([IO.Path]::GetTempPath()) ('datalens-bootstrap-' + [Guid]::NewGuid())
New-Item -ItemType Directory $Stage | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Uv = "$BinDir/uv.exe"
$Yc = "$BinDir/yc.exe"
function Invoke-Uv {
    & $Uv --no-config @args
    if ($LASTEXITCODE -ne 0) { throw "uv failed with exit code $LASTEXITCODE." }
}

try {
    $UvReady = $false
    if (Test-Path $Uv) {
        $Installed = & $Uv --version
        $UvReady = $LASTEXITCODE -eq 0 -and $Installed -like "uv $UvVersion *"
    }
    if (!$UvReady) {
        $Archive = Join-Path $Stage 'uv.zip'
        Invoke-WebRequest -UseBasicParsing "https://releases.astral.sh/github/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $Archive
        Expand-Archive $Archive -DestinationPath (Join-Path $Stage 'uv')
        $Binary = Get-ChildItem (Join-Path $Stage 'uv') -Recurse -Filter 'uv.exe' | Select-Object -First 1
        if (!$Binary) { throw 'uv.exe is missing from the downloaded archive.' }
        & $Binary.FullName --version
        if ($LASTEXITCODE -ne 0) { throw 'Downloaded uv failed its version check.' }
        Copy-Item $Binary.FullName $Uv -Force
    }
    $YcReady = $false
    if (Test-Path $Yc) {
        $Installed = & $Yc version
        $YcReady = $LASTEXITCODE -eq 0 -and $Installed -like "*CLI $YcVersion *"
    }
    if (!$YcReady) {
        $Binary = Join-Path $Stage 'yc.exe'
        Invoke-WebRequest -UseBasicParsing "https://storage.yandexcloud.net/yandexcloud-yc/release/$YcVersion/windows/amd64/yc.exe" -OutFile $Binary
        & $Binary version
        if ($LASTEXITCODE -ne 0) { throw 'Downloaded yc failed its version check.' }
        Copy-Item $Binary $Yc -Force
    }

    $env:UV_CACHE_DIR = Join-Path $RuntimeRoot '.cache/uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $ToolDir 'python'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $RuntimeRoot '.venv'
    $env:UV_PYTHON_INSTALL_BIN = '0'
    $env:UV_PYTHON_NO_REGISTRY = '1'
    Invoke-Uv python install $PythonVersion
    Invoke-Uv venv --clear --relocatable --managed-python --python $PythonVersion $env:UV_PROJECT_ENVIRONMENT
    Invoke-Uv sync --frozen --managed-python --python $PythonVersion
    $RuntimePython = Join-Path $RuntimeRoot '.venv/Scripts/python.exe'
    Invoke-Uv pip check --python $RuntimePython
    & $RuntimePython -B -c 'import importlib, os; importlib.import_module(os.environ.get("DATALENS_BOOTSTRAP_MODULE", "analytics_toolkit.datalens_utils.bootstrap")).check_environment(os.getcwd(), os.environ["DATALENS_RUNTIME_DIR"])'
    if ($LASTEXITCODE -ne 0) { throw 'Setup checks failed.' }
    Write-Host 'Setup complete. If this machine is not signed in, run:'
    & $RuntimePython -B -c 'import importlib, os; setup = importlib.import_module(os.environ.get("DATALENS_BOOTSTRAP_MODULE", "analytics_toolkit.datalens_utils.bootstrap")); print(str(setup.managed_yc_binary(os.environ["DATALENS_RUNTIME_DIR"])) + " init --profile " + (os.environ.get("DATALENS_YC_PROFILE") or "YOUR_PROFILE"))'
    Write-Host 'Then: python dashboard.py'
} finally {
    Remove-Item $Stage -Recurse -Force
}
