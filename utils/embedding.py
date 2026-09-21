import os
from openai import OpenAI

openai_api_key = os.getenv("OPENAI_API_KEY")

openai_client = OpenAI(api_key=openai_api_key)

def gpt_embedding(text: str, model_name: str = "text-embedding-3-small") -> list[float]:
    """
    Menghasilkan embedding vektor dari input teks.

    Args:
        text (str): Teks yang akan dikonversi menjadi embedding.
        model_name (str): Nama model GPT-5 untuk embedding (default: text-embedding-3-small).

    Returns:
        list: Vektor embedding dalam bentuk list float.
    """
    try:
        result = openai_client.embeddings.create(
            model=model_name,
            input=text,
            encoding_format="float",
        )
        return result.data[0].embedding

    except Exception as e:
        print(f"[ERROR] Error saat membuat embedding: {e}")
        return []
