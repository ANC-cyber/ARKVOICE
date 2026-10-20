# ArkVoice MVP

ArkVoice 是一个面向 Windows 的个人实验工具：在游玩《明日方舟》时读取窗口中的中文剧情台词，按本地剧情脚本定位当前句，调用翻译接口生成日文，再通过本机 GPT-SoVITS 合成并播放。程序会预载后续语音，并在游戏窗口旁显示角色趴趴图和滚动运行日志。

这是社区交流与娱乐用途的原型，不是《明日方舟》官方产品。

## 功能

- 捕获前台明日方舟窗口下方的台词区域；启动时尝试 OCR 匹配当前剧情句，之后结合区域变化与游戏内鼠标点击推进脚本。
- 中文台词可通过 OpenAI Chat Completions 兼容 API 翻译成日语；已有日文原文时直接使用。
- 调用本机 GPT-SoVITS v5turbo API 合成语音，缓存翻译和音频并预载后续句子。
- 按 speaker 映射角色形象；可为每个角色配置独立的参考音频、逐字稿和情感候选。
- 小人吸附在游戏窗口上沿；鼠标悬停显示前进/后退按钮。日志面板吸附在游戏窗口左侧，可滚轮查看历史。

## 快速开始

### 1. 准备 GPT-SoVITS

本仓库不包含 GPT-SoVITS、模型权重或参考音频。请单独安装并启动一个支持 v5turbo 的 GPT-SoVITS 整合包；确认该整合包可通过 `api_v2.py` 启动 `/tts` 接口。

### 2. 配置本机路径

复制 `settings.local.example.bat` 为 `settings.local.bat`，编辑其中的路径：

- `ARKVOICE_SOVITS_DIR`：GPT-SoVITS 整合包根目录，目录内应有 `runtime\python.exe`、`api_v2.py`。
- `ARKVOICE_TTS_CONFIG`：v5turbo 推理配置文件。
- `ARKVOICE_REFERENCE_AUDIO`：你有权使用的参考音频。
- `ARKVOICE_REFERENCE_TEXT_FILE`：UTF-8 编码的日文逐字稿文件，默认是 `voices/reference.txt`。

参考音频应清晰、干净，并与逐字稿完全一致。克隆他人声音前，请确认已取得本人授权，并遵守模型、数据及平台规则。

将 `voices/reference.txt.example` 复制为 `voices/reference.txt`，用 UTF-8 编辑成参考音频的准确逐字稿。参考音频本身不要放入公开仓库。

### 3. 安装运行依赖

在 PowerShell 中运行：

```powershell
Copy-Item settings.local.example.bat settings.local.bat
# 编辑 settings.local.bat 后：
powershell -ExecutionPolicy Bypass -File .\setup_ocr.ps1
```

安装脚本会从 `settings.local.bat` 读取 GPT-SoVITS Python，并把 ArkVoice 依赖装到本目录的 `.deps`。首次运行 RapidOCR 时可能下载 OCR 模型。

### 4. 启动

双击 `run_mvp.bat`。首次启动会打开参数窗口：

1. 填写 OpenAI Chat Completions 兼容翻译 API 地址、模型和 API Key。
2. 准备自己的剧情 JSON 并放入 `data/stories/`，格式参考该目录中的说明。
3. 选择剧情脚本后点击“保存并开始监听”，将游戏切到前台。

API Key 由 Windows 当前用户使用 DPAPI 加密，写入 `logs/console-settings.json`。此文件和日志、缓存均被 `.gitignore` 排除，不要手动把它们加入 Git。

## 剧情数据

仓库不附带《明日方舟》的剧情文本、游戏资源或解包产物。请只导入你有权使用的本地数据。JSON 的基本结构：

```json
{
  "stage_id": "MY-STORY-01",
  "title": "我的测试剧情",
  "lines": [
    {"id": "MY-STORY-01-0001", "speaker": "角色名", "zh_CN": "中文台词", "ja_JP": ""},
    {"id": "MY-STORY-01-0002", "speaker": "", "zh_CN": "旁白或停顿", "ja_JP": ""}
  ]
}
```

文件名和 `stage_id` 应一致，例如 `MY-STORY-01.json`。`speaker` 为空的行会作为剧情停顿，不生成语音，但仍占用一次剧情推进。若 `ja_JP` 已填写，程序跳过翻译 API。

## 角色与声音配置

编辑 `data/characters.json`：用 `speaker_aliases` 把脚本 speaker 映射到角色，用 `sprite` 指定 `assets/` 下的图片。默认参考音频通过 `settings.local.bat` 设置。想给不同角色使用不同音色，可在对应角色的 `reference_candidates` 中配置：

```json
{
  "id": "my_reference_01",
  "audio": "voices/my_reference.wav",
  "japanese_text": "与参考音频完全一致的日文逐字稿",
  "emotion_label": "calm",
  "selection_keywords": ["安心", "静か"]
}
```

参考音频文件由使用者自行放入；`voices/` 默认被 Git 忽略。不要把未获授权的角色语音、个人录音或训练数据提交到公开仓库。

## 隐私与文件说明

- 翻译时，台词文本会发送给你配置的第三方翻译 API；具体处理方式以该服务的政策为准。
- API Key 使用 Windows DPAPI 加密后保存在本机 `logs/console-settings.json`。
- OCR、TTS 日志、截图、翻译缓存、音频缓存和模型文件都在本地生成，并被 Git 忽略。
- GPT-SoVITS 和 OCR 模型需由使用者自行安装/下载；本仓库不重新分发模型、语音或游戏数据。

## 已知限制

- 目前只支持 Windows 桌面窗口；独占全屏、特殊渲染模式或系统缩放设置可能导致捕获/叠加层不可用。
- OCR 仅用于启动定位和识别失败后的校准；平时主要靠台词框颜色变化与点击检测推进，分支或跳句可能造成错位。
- 生成速度受本机显卡、GPT-SoVITS 模型和翻译 API 延迟影响；预载可减少等待，但不能保证每句零延迟。
- 翻译质量由所配置的服务决定，游戏术语可能需要自行修订。

## 致谢

- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)：本地语音合成；该项目采用 MIT 许可证，本仓库不包含其源码或模型。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：文字识别；该项目采用 Apache-2.0 许可证，本仓库通过安装脚本获取依赖和模型。
- [PRTS Wiki](https://prts.wiki/)：角色语音资料研究与参考；本仓库不附带从 Wiki 获取的音频。
- [AyaKumo/arknights-story-reader](https://github.com/AyaKumo/arknights-story-reader)：剧情数据研究参考；本仓库未复制其剧情文本，也未假定其数据具有再分发许可。
- 感谢 Arknights 玩家社区维护的资料和工具。

## 免责声明与权利说明

本项目仅供个人学习、技术交流和娱乐研究。它与 Hypergryph、Yostar 或《明日方舟》官方没有关联，也未获其背书。

《明日方舟》名称、角色、剧情、语音及相关游戏素材的权利归其各自权利人所有。仓库中的角色趴趴图属于非官方同人/衍生展示素材，不代表取得了角色或原作素材的授权。请勿将本项目或输出用于商业用途、误导性宣传、冒充官方或侵害他人权益；公开传播前，请自行确认所用图片、声音、文本、模型及输出的授权范围，并遵守当地法律及相关服务条款。

软件按现状提供，不对可用性、翻译准确性、生成音频的权利状态或适用性作保证。使用者应自行承担使用、分享和发布所产生的责任；收到权利人要求时，应及时停止相关使用并移除对应素材。

## 许可证

本仓库未声明对《明日方舟》角色图片、游戏内容、模型或音频授予任何许可证。第三方依赖按各自许可证使用。若要对 ArkVoice 自有代码授予开源许可证，请在公开前添加你选择的许可证文本。
