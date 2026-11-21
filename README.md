# NoxGate GPT Image 技能

这是一个独立的 Codex 技能，通过当前 Codex provider 配置调用 NoxGate GPT Image API，支持模型和费率查询、图片生成与参考图编辑。

## 安装

```bash
git clone https://github.com/noxgate/skills.git
mkdir -p ~/.codex/skills
cp -R skills/noxgate-gpt-image ~/.codex/skills/
```

安装后重启 Codex，技能会以 `$noxgate-gpt-image` 出现在技能列表中。

## 使用要求

当前 Codex provider 的 Base URL 需要是 `https://noxgate.net/v1`。技能直接读取该 provider 的现有凭据，不会要求单独绑定 API Key。模型选择、生成和编辑用法见 [技能说明](noxgate-gpt-image/SKILL.md)。
