# GPU ジョブのイメージ（未ビルド・未確認）。vLLM のイメージを土台に、gpu_worker.py だけを入れる。
# 土台のタグは、使う vLLM の版に固定して --build-arg で渡す（既定は置かない。Qwen3.6 のカードは vllm>=0.19.0 を示す）。
#   例: docker build --build-arg VLLM_IMAGE=<registry>/vllm-openai:<固定したタグ> -f evals/synth/gpu/Dockerfile evals/synth
ARG VLLM_IMAGE
FROM ${VLLM_IMAGE}
WORKDIR /app
COPY gpu_worker.py /app/gpu_worker.py
# 土台の ENTRYPOINT（API サーバー）を、ワーカーに置き換える。引数は、ジョブの実行ごとに渡す
ENTRYPOINT ["python3", "/app/gpu_worker.py"]
