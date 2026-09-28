# push.ps1 -- 一键把 fw360 目录推送到 GitHub，触发 Actions 自动解包
#
# 用法（首次）:
#   1. 先去 github.com 建一个空仓库（不要勾选 README/.gitignore）
#   2. 在本目录运行:
#        .\push.ps1 -RepoUrl https://github.com/<你的用户名>/<仓库名>.git
#
# 以后更新再推:  .\push.ps1

param(
    [string]$RepoUrl = "",
    [string]$Message = "extract 360T7 firmware"
)

Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[!] 未找到 git，请先安装 Git for Windows" -ForegroundColor Red
    exit 1
}

# 1) init
if (-not (Test-Path .git)) {
    git init | Out-Null
    git branch -M main 2>$null
    Write-Host "[+] git init 完成"
}

# 2) remote（git remote 无 remote 时不输出 stderr / 退出码 0）
$remotes = git remote
$hasOrigin = $LASTEXITCODE -eq 0 -and ($remotes -contains 'origin')

if ($RepoUrl) {
    if ($hasOrigin) { git remote set-url origin $RepoUrl } else { git remote add origin $RepoUrl }
    Write-Host "[+] origin = $RepoUrl"
} elseif (-not $hasOrigin) {
    Write-Host "[!] 还没有 remote。请先在 github.com 建一个空仓库，然后运行：" -ForegroundColor Yellow
    Write-Host "    .\push.ps1 -RepoUrl https://github.com/<你的用户名>/<仓库名>.git" -ForegroundColor Yellow
    exit 1
}

# 3) commit
git add -A
git commit -m $Message | Out-Null
if ($LASTEXITCODE -ne 0) {
    # 无全局 identity 时兜底；或没有变更
    git -c user.name="fw360" -c user.email="fw360@local" commit -m $Message | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "[i] 没有新变更需要提交" }
}

# 4) push（首次会弹 GitHub 登录窗口，走 Git Credential Manager）
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] push 失败：请确认仓库已创建且账号有推送权限" -ForegroundColor Red
    exit 1
}

# 5) 提示 Actions 地址
$url = (git remote get-url origin) -replace '\.git$', ''
Write-Host ""
Write-Host "[+] 推送成功！Actions 正在云端解包固件" -ForegroundColor Green
Write-Host "    查看进度: $url/actions"
Write-Host "    完成后在该次 run 页面底部下载 artifact: 360t7-extracted"
