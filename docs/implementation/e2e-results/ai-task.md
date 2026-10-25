# LLMM-023 — AI Task `generate_data` live rows

**Status: all rows NOT RUN (owner-run).** I tried to run the Ollama row from the
devcontainer, but the `ollama/ollama:latest` amd64 image is ~3.4 GB compressed, over
the ~2 GB pull budget for agent sessions. No other live backend was available.

Unit coverage for every acceptance criterion is in `tests/test_ai_task.py` and
`tests/test_config_flow_ai_task.py`. Those tests drive HA's real `ai_task.generate_data`
service with only the HTTP boundary mocked, on both CI legs (HA 2026.10 locked and the
2026.7 floor).

## Recipe

1. Bring up a throwaway HA (`scripts/e2e/README.md`). Install this branch's
   `custom_components/llm_middleman`.
2. Add an **OpenAI-compatible** connection and an **Ollama** connection. On each one,
   choose **Add AI task** and pick a model. For the attachment row, use a vision model
   such as `llava` or `qwen2.5vl` on Ollama, or `gpt-4o-mini` on OpenAI.
3. In **Developer tools → Actions**, run each row and paste the response below.

```yaml
# Row 1: plain text
action: ai_task.generate_data
data:
  task_name: Greeting
  entity_id: ai_task.llm_middleman_ai_task   # the entity you created
  instructions: Say hello in five words.
response_variable: result
```

```yaml
# Row 2: structured output
action: ai_task.generate_data
data:
  task_name: Weather summary
  entity_id: ai_task.llm_middleman_ai_task
  instructions: Invent a weather report for today.
  structure:
    temperature:
      selector: {number: {}}
      required: true
    summary:
      selector: {text: {}}
      required: true
```

```yaml
# Row 3: image attachment (vision model)
action: ai_task.generate_data
data:
  task_name: Describe image
  entity_id: ai_task.llm_middleman_ai_task
  instructions: Describe this image in one sentence.
  attachments:
    - media_content_id: media-source://media_source/local/test.jpg
      media_content_type: image/jpeg
```

## Rows

| # | Preset | Row | Expected | Result |
|---|---|---|---|---|
| 1 | openai_compat | plain text | `data` is a string reply | NOT RUN |
| 2 | openai_compat | structured | `data` is `{temperature: <number>, summary: <string>}` | NOT RUN |
| 3 | openai_compat | image attachment | the reply describes the image | NOT RUN |
| 4 | ollama | plain text | `data` is a string reply | NOT RUN |
| 5 | ollama | structured | `data` is `{temperature: <number>, summary: <string>}` | NOT RUN |
| 6 | ollama | image attachment | the reply describes the image | NOT RUN |
| 7 | openai_compat | structured, server without `json_schema` support | translated "The AI task failed: …" error, with the server's 4xx body in the HA log | NOT RUN |
