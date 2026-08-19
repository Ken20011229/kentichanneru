"""Groq のモデルごとに違う呼び出し方を1箇所にまとめる。

2026-08-17 に llama-3.3-70b-versatile が Groq から廃止され、代替を選ぶ段で
「思考の止め方も JSON の要求方法もモデルごとに違う」ことが実際に問題になった
(qwen は json_object に thinking が混ざって 400、gpt-oss は json_object 自体が
400)。モデルを差し替えるたびに呼び出し側を直して回らずに済むよう、差分は
ここだけに置く。
"""


def call_kwargs(model: str, want_json: bool = False) -> dict:
    """chat.completions.create にそのまま渡せる追加引数を返す。

    - gpt-oss 系: response_format={"type":"json_object"} を付けると
      400 json_validate_failed。JSON はプロンプトで要求して自前でパースする
      (```json フェンス付きで返ることがあるので _parse_json_lenient を使う)。
      reasoning_effort を既定のままにすると推論だけで completion 上限を使い切り、
      content が空のまま finish_reason="length" で返る(実測: 推論 3,070 トークン)。
      "low" なら推論は約 38 トークンに収まる。
    - qwen3 系: 思考を出したまま JSON を要求すると <think> が本文に混ざって
      400 json_validate_failed になる。reasoning_effort="none" で思考を止めれば
      json_object がそのまま通る("low" は 400。許容値は "none" か "default" のみ)。
    """
    kwargs: dict = {}
    if "gpt-oss" in model:
        kwargs["reasoning_effort"] = "low"
        return kwargs
    if "qwen" in model:
        kwargs["reasoning_effort"] = "none"
    if want_json:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs
