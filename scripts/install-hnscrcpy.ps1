# 外置安装 hnscrcpy（鸿蒙 NEXT 投屏工具）到项目 .tools/hnscrcpy。
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts/install-hnscrcpy.ps1
# 特点：
#   - 完整性校验：SHA256 不符/大小不对会重新下载（本机代理不支持 HTTP range，故不续传）。
#   - 静默安装：NSIS 安装包用 /S 静默装到 .tools/hnscrcpy，避免 UAC 交互。
#   - 校验 SHA256（官方 release 提供的值）。
#   - 不随产品分发：hosScrcpy/hdc 版权归华为，仅供本机开发调试。
#
# 安装后控制台会自动识别（hnscrcpy_exe 候选含 .tools/hnscrcpy），
# 也可用环境变量 HNSCRCPY_EXE 指定任意位置。

$ErrorActionPreference = 'Stop'
$version = 'v1.0.3'
$url = "https://github.com/taxiao213/hnscrcpy/releases/download/$version/hnscrcpy-v1.0.3-windows-x64-setup.exe"
$expectSha = '4556f711af5f9fffcb11a69a02dc3111d4d925be4897e66ed19221e8b1d0d938'
$expectSize = 113098240
$root = Split-Path -Parent $PSScriptRoot
$dlDir = Join-Path $root '.tools\.downloads'
$installer = Join-Path $dlDir "hnscrcpy-$version-windows-x64-setup.exe"
$target = Join-Path $root '.tools\hnscrcpy'

New-Item -ItemType Directory -Force -Path $dlDir | Out-Null
$curl = 'curl.exe'
$curlPath = Get-Command $curl -ErrorAction SilentlyContinue
if (-not $curlPath) { $curl = Join-Path $env:WINDIR 'System32\curl.exe' }

Write-Output "hnscrcpy $version 外置安装"
Write-Output "下载: $url"
Write-Output "目标: $target"

# 下载/续传：存在且未完成时用 -C - 续传
$needDownload = $true
if (Test-Path $installer) {
    $cur = (Get-Item $installer).Length
    if ($cur -eq $expectSize) {
        $sha = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
        if ($sha -eq $expectSha) {
            Write-Output "安装包已完整且校验通过 ($cur bytes)，跳过下载"
            $needDownload = $false
        } else {
            Write-Output "安装包大小正确但 SHA 不符，重新下载"
            Remove-Item $installer -Force
        }
    } else {
        Write-Output "已有不完整文件 $cur / $expectSize bytes（本机代理不支持续传，将重新下载）"
        Remove-Item $installer -Force
    }
}

if ($needDownload) {
    # 注意：本机 github 本地代理不支持 HTTP range（curl -C - 会 SSL 错误），故直接整包下载。
    $args = @('-L', '--fail', '--retry', '5', '-o', $installer, $url)
    & $curl @args
    if ($LASTEXITCODE -ne 0) { throw "下载失败 (curl exit=$LASTEXITCODE)，可重跑本脚本续传" }
    $got = (Get-Item $installer).Length
    if ($got -ne $expectSize) { throw "下载不完整: $got / $expectSize，重跑本脚本续传" }
    $sha = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLower()
    if ($sha -ne $expectSha) { throw "SHA256 校验失败: $sha" }
    Write-Output "下载完成并校验通过"
}

# 静默安装（NSIS /S）。若已安装则跳过。
$exe = Get-ChildItem -Path $target -Filter 'hnscrcpy.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($exe) {
    Write-Output "已安装: $($exe.FullName)"
} else {
    Write-Output "静默安装到 $target ..."
    $p = Start-Process -FilePath $installer -ArgumentList @('/S', "/D=$target") -Wait -PassThru
    Write-Output "安装进程 exit=$($p.ExitCode)"
    $exe = Get-ChildItem -Path $target -Filter 'hnscrcpy.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $exe) { throw "安装后未找到 hnscrcpy.exe，请手动运行 $installer 查看" }
    Write-Output "安装完成: $($exe.FullName)"
}

Write-Output ""
Write-Output "完成。控制台会自动检测；若未识别，设置环境变量："
Write-Output "  `$env:HNSCRCPY_EXE = '$($exe.FullName)'"
