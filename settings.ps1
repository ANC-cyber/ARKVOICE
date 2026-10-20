$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Security

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $root 'logs\console-settings.json'
$statePath = Join-Path $root 'logs\console-state.json'
$storyDir = Join-Path $root 'data\stories'
$current = @{}
if (Test-Path $configPath) { try { $current = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json } catch {} }
function Get-Value($name, $fallback) {
    $property = $current.PSObject.Properties[$name]
    if ($null -ne $property -and $null -ne $property.Value) { return $property.Value }
    return $fallback
}
function Protect-Text([string]$text) {
    if ([string]::IsNullOrEmpty($text)) { return '' }
    $bytes = [Text.Encoding]::UTF8.GetBytes($text)
    return [Convert]::ToBase64String([Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
}
function Unprotect-Text([string]$text) {
    if ([string]::IsNullOrEmpty($text)) { return '' }
    try {
        $bytes = [Convert]::FromBase64String($text)
        return [Text.Encoding]::UTF8.GetString([Security.Cryptography.ProtectedData]::Unprotect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
    } catch { return '' }
}

$form = New-Object Windows.Forms.Form
$form.Text = 'ArkVoice · 配音参数'
$form.Size = New-Object Drawing.Size(650, 570)
$form.MinimumSize = New-Object Drawing.Size(650, 570)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 9)
$form.TopMost = $true

function Add-Label($text, $x, $y, $w = 155) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $text; $label.Location = New-Object Drawing.Point($x, $y); $label.Size = New-Object Drawing.Size($w, 25)
    $form.Controls.Add($label)
}
function Add-TextBox($name, $x, $y, $w, $value, $masked = $false) {
    $box = New-Object Windows.Forms.TextBox
    $box.Name = $name; $box.Location = New-Object Drawing.Point($x, $y); $box.Size = New-Object Drawing.Size($w, 26); $box.Text = [string]$value
    if ($masked) { $box.UseSystemPasswordChar = $true }
    $form.Controls.Add($box); return $box
}

Add-Label '剧情关卡' 24 22
$stage = New-Object Windows.Forms.ComboBox
$stage.Location = New-Object Drawing.Point(180, 18); $stage.Size = New-Object Drawing.Size(420, 28); $stage.DropDownStyle = 'DropDownList'
Get-ChildItem $storyDir -Filter '*.json' | ForEach-Object {
    try { $story = Get-Content -Raw -Encoding UTF8 $_.FullName | ConvertFrom-Json; [void]$stage.Items.Add("$($story.title)  ·  $($_.BaseName)") } catch {}
}
if ($stage.Items.Count -eq 0) { [void]$stage.Items.Add('主线 · 14-1  ·  MAIN-14-01') }
$selectedStage = [string](Get-Value 'stage_id' 'MAIN-14-01')
for ($i = 0; $i -lt $stage.Items.Count; $i++) { if ([string]$stage.Items[$i] -match "·\s*$([regex]::Escape($selectedStage))$") { $stage.SelectedIndex = $i } }
if ($stage.SelectedIndex -lt 0) { $stage.SelectedIndex = 0 }
$form.Controls.Add($stage)

Add-Label '翻译 API 地址' 24 66
$endpoint = Add-TextBox 'endpoint' 180 62 420 (Get-Value 'api_base' 'https://api.openai.com/v1/chat/completions')
Add-Label '模型名称' 24 108
$model = Add-TextBox 'model' 180 104 420 (Get-Value 'model' 'gpt-4o-mini')
Add-Label 'API Key（本机加密保存）' 24 150 155
$apiKey = Add-TextBox 'apiKey' 180 146 420 (Unprotect-Text ([string](Get-Value 'api_key_protected' ''))) $true

Add-Label '语音速度' 24 198
$speed = New-Object Windows.Forms.NumericUpDown
$speed.Location = New-Object Drawing.Point(180, 194); $speed.Size = New-Object Drawing.Size(120, 26); $speed.Minimum = 0.6; $speed.Maximum = 1.5; $speed.DecimalPlaces = 2; $speed.Increment = 0.05; $speed.Value = [decimal](Get-Value 'speed' 1.0)
$form.Controls.Add($speed)
Add-Label '监听台词框底部高度（%）' 320 198 190
$region = New-Object Windows.Forms.NumericUpDown
$region.Location = New-Object Drawing.Point(510, 194); $region.Size = New-Object Drawing.Size(90, 26); $region.Minimum = 5; $region.Maximum = 30; $region.Value = [decimal](Get-Value 'dialogue_bottom_percent' 10)
$form.Controls.Add($region)
Add-Label '台词变化灵敏度' 24 238
$threshold = New-Object Windows.Forms.NumericUpDown
$threshold.Location = New-Object Drawing.Point(180, 234); $threshold.Size = New-Object Drawing.Size(120, 26); $threshold.Minimum = 0.0001; $threshold.Maximum = 0.03; $threshold.DecimalPlaces = 4; $threshold.Increment = 0.0001; $threshold.Value = [decimal](Get-Value 'change_threshold' 0.0008)
$form.Controls.Add($threshold)

$liveTitle = New-Object Windows.Forms.Label
$liveTitle.Text = '小人实时播报'; $liveTitle.Font = New-Object Drawing.Font('Microsoft YaHei UI', 10, [Drawing.FontStyle]::Bold); $liveTitle.Location = New-Object Drawing.Point(24, 285); $liveTitle.Size = New-Object Drawing.Size(180, 25); $form.Controls.Add($liveTitle)
$liveStatus = New-Object Windows.Forms.Label
$liveStatus.Text = '正在读取控制台状态…'; $liveStatus.Location = New-Object Drawing.Point(24, 318); $liveStatus.Size = New-Object Drawing.Size(580, 28); $liveStatus.ForeColor = [Drawing.Color]::DarkSlateBlue; $form.Controls.Add($liveStatus)
$liveLine = New-Object Windows.Forms.Label
$liveLine.Text = ''; $liveLine.Location = New-Object Drawing.Point(24, 350); $liveLine.Size = New-Object Drawing.Size(580, 72); $liveLine.AutoEllipsis = $true; $form.Controls.Add($liveLine)
$help = New-Object Windows.Forms.Label
$help.Text = '游戏窗口左侧的滚动日志面板会显示等待窗口、翻译、合成与播放进度。此窗口可在剧情播放时保持打开。'; $help.Location = New-Object Drawing.Point(24, 430); $help.Size = New-Object Drawing.Size(580, 42); $help.ForeColor = [Drawing.Color]::DimGray; $form.Controls.Add($help)

function Save-Settings([bool]$listening) {
    $stageId = [string]$stage.SelectedItem
    if ($stageId -match '·\s*([A-Za-z0-9_-]+)\s*$') { $stageId = $Matches[1] } else { $stageId = 'MAIN-14-01' }
    try { $protectedKey = Protect-Text $apiKey.Text } catch {
        [Windows.Forms.MessageBox]::Show('Windows 当前会话无法加密 API Key，请确认已登录 Windows 用户桌面后重试。', '无法保存 API Key', 'OK', 'Error') | Out-Null
        return
    }
    $payload = @{
        stage_id = $stageId; api_base = $endpoint.Text.Trim(); model = $model.Text.Trim(); api_key_protected = $protectedKey
        speed = [double]$speed.Value; dialogue_bottom_percent = [int]$region.Value; change_threshold = [double]$threshold.Value; listening = $listening
    }
    $parent = Split-Path -Parent $configPath
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $temp = "$configPath.tmp"
    [IO.File]::WriteAllText($temp, ($payload | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
    Move-Item -Force $temp $configPath
    $form.Close()
}
$start = New-Object Windows.Forms.Button
$start.Text = '保存并开始监听'; $start.Location = New-Object Drawing.Point(330, 480); $start.Size = New-Object Drawing.Size(140, 36); $start.Add_Click({ Save-Settings $true }); $form.Controls.Add($start)
$pause = New-Object Windows.Forms.Button
$pause.Text = '保存并暂停'; $pause.Location = New-Object Drawing.Point(480, 480); $pause.Size = New-Object Drawing.Size(120, 36); $pause.Add_Click({ Save-Settings $false }); $form.Controls.Add($pause)

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 500
$timer.Add_Tick({
    try {
        if (Test-Path $statePath) {
            $state = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
            $liveStatus.Text = [string]$state.status
            $liveLine.Text = [string]$state.line
        }
    } catch {}
})
$timer.Start()
[void]$form.ShowDialog()
$timer.Stop()
