import torch
from sentence_transformers import SentenceTransformer

MODEL_PATH = r"D:\rag_system\models\Qwen3-Embedding-0.6B"

device = "cuda" if torch.cuda.is_available() else "cpu"
print("当前设备：", device)

model = SentenceTransformer(MODEL_PATH, device=device)

query = ["铸钢件出现缩孔缺陷时，应该从哪些工艺参数进行预防？"]

documents = [
    "缩孔缺陷通常与补缩不足、冒口设计不合理、浇注温度过高、凝固顺序不合理等因素有关。",
    "砂型强度不足可能导致夹砂、冲砂、表面粗糙等缺陷。",
    "浇注系统设计应保证金属液平稳充型，减少紊流、卷气和夹杂。"
]

query_embeddings = model.encode(
    query,
    prompt_name="query",
    normalize_embeddings=True
)

doc_embeddings = model.encode(
    documents,
    normalize_embeddings=True
)

scores = query_embeddings @ doc_embeddings.T

print("query embedding shape:", query_embeddings.shape)
print("doc embedding shape:", doc_embeddings.shape)
print("相似度分数：")
print(scores)