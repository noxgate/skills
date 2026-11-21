---
name: noxgate-gpt-image
description: 当当前 Codex provider 的 Base URL 是 https://noxgate.net/v1 且用户要求生成、绘制、重绘或编辑图片时使用。直接复用当前 Codex 配置中的 API Key 调用 GPT Image，不要求额外绑定。用户未指定模型时必须先展示当前 API Key 可用的 GPT Image 模型、费率和预估成本并等待选择，只有用户明确说使用默认模型时才使用 gpt-image-2.6-flare。
---

# NoxGate GPT Image

这是一个轻量的独立图片生成技能。用户只安装本技能时也必须可以完成模型查询、图片生成和参考图编辑，不依赖 NoxGate 插件、NoxGate profile 或第三方 Python 包。

## 触发和认证

仅在当前 Codex provider 的 `base_url` 精确归一化为 `https://noxgate.net/v1` 时调用本技能。脚本从 `$CODEX_HOME/config.toml` 读取当前 `model_provider` 对应的 provider，并读取其 `experimental_bearer_token`，没有该字段时按 `env_key` 从环境变量读取。不要显示、记录、复制或写入图片元数据中的 API Key。

如果 Base URL 不是上述地址，说明本技能不适用，并让用户使用与其 provider 对应的图片工具。不要修改 Codex 配置，也不要要求用户再次绑定 API Key。

## 模型选择

- 用户明确指定模型 ID 或模型名称时，先查询当前授权模型并核对该模型支持本次生成或编辑，再提交请求。
- 用户明确说「使用默认模型」或等价表达时，使用 `gpt-image-2.6-flare`。仍需先核对当前 API Key 是否授权该模型，不可用时展示可选模型。
- 用户没有指定模型时，先运行模型查询，展示当前 API Key 可用的 GPT Image 模型、模型 ID、目录基础费率和本次预估费用。默认优先展示最新版本，保留「查看全部模型」入口。即使只有一个候选，也必须等待用户明确选择后再生成。
- Codex 提供 `request_user_input_async` 时，用它创建「请选择图片模型」的可点击选项，每个选项包含模型名称、模型 ID 和费用，并把选择映射为准确的模型 ID。用户选择「查看全部模型」时，从查询结果的完整 `models` 列表继续提供选项。没有该工具时展示带编号的同一批候选，接受编号或模型 ID。
- 目录基础费率仅用于估算，实际费用以结算为准。费率缺失时显示「暂无报价」。

查询模型：

```bash
python3 "$SKILL_ROOT/scripts/noxgate_gpt_image.py" models --operation generate --count 1
```

生成图片：

```bash
python3 "$SKILL_ROOT/scripts/noxgate_gpt_image.py" generate \
  --prompt "描述图片内容" \
  --model "用户选定的模型 ID" \
  --output "/tmp/noxgate-gpt-image.png"
```

用户明确要求默认模型时：

```bash
python3 "$SKILL_ROOT/scripts/noxgate_gpt_image.py" generate \
  --prompt "描述图片内容" \
  --use-default \
  --output "/tmp/noxgate-gpt-image.png"
```

## 图片编辑

用户提供本地参考图时使用 `edit`，将每张图片通过 `--image` 传入。需要遮罩时使用 `--mask`。编辑同样遵循模型选择规则，未指定模型时先查询 `--operation edit`。

```bash
python3 "$SKILL_ROOT/scripts/noxgate_gpt_image.py" edit \
  --prompt "保留构图，将背景改成夜景" \
  --image "/absolute/path/reference.png" \
  --model "用户选定的模型 ID" \
  --output "/tmp/noxgate-gpt-image-edited.png"
```

## 返回结果

成功后确认输出文件存在，并用绝对路径展示图片，例如 `![生成结果](/tmp/noxgate-gpt-image.png)`。提交超时或结果不确定时报告错误，不自动重新提交可能产生费用的请求。
