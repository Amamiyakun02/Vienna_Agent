import os
import time
import tempfile
import numpy as np
import fitz  # PyMuPDF
from typing import List, Optional
from fastapi import APIRouter, UploadFile, File, Query, HTTPException, status, Response
from pydantic import BaseModel

router = APIRouter(prefix="/v1/pdf", tags=["PDF Analysis"])

class PDFPageDetail(BaseModel):
    page: int
    is_colored: bool

class PDFMetadata(BaseModel):
    title: str
    author: str
    subject: str
    keywords: str
    creator: str
    producer: str
    creationDate: str
    modDate: str
    format: str
    encryption: str

class PDFColorAnalysisResponse(BaseModel):
    status: str
    filename: str
    file_size_bytes: int
    file_size_formatted: str
    total_pages: int
    colored_pages_count: int
    grayscale_pages_count: int
    colored_pages: List[int]
    grayscale_pages: List[int]
    threshold_used: int
    detection_time_seconds: float
    metadata: PDFMetadata
    pages_details: List[PDFPageDetail]

def format_file_size(size_in_bytes: int) -> str:
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.2f} KB"
    else:
        return f"{size_in_bytes / (1024 * 1024):.2f} MB"

@router.post("/check-color", response_model=PDFColorAnalysisResponse)
async def check_pdf_color(
    file: UploadFile = File(...),
    threshold: int = Query(15, ge=0, le=255, description="Batas toleransi perbedaan warna RGB untuk dianggap berwarna (0-255)")
):
    # 1. Validasi tipe file
    # Pastikan file adalah PDF berdasarkan ekstensi file
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tipe file tidak valid. Hanya file PDF yang diperbolehkan."
        )
        
    if file.content_type and file.content_type != "application/pdf":
        if file.content_type != "application/octet-stream":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tipe content-type tidak valid. Hanya dokumen PDF yang diperbolehkan."
            )

    # 2. Simpan file ke direktori sementara (temp)
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    temp_path = temp_file.name
    
    try:
        # Tulis konten file ke temp
        content = await file.read()
        file_size = len(content)
        temp_file.write(content)
        temp_file.close()
        
        # 3. Analisis warna PDF
        start_time = time.time()
        
        try:
            doc = fitz.open(temp_path)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Gagal membuka dokumen PDF. File mungkin rusak atau tidak valid: {str(e)}"
            )
            
        try:
            if doc.is_encrypted:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="File PDF terenkripsi atau membutuhkan kata sandi."
                )
                
            total_pages = len(doc)
            colored_pages = []
            grayscale_pages = []
            pages_details = []
            
            for i in range(total_pages):
                page = doc.load_page(i)
                # Rendam ke pixmap resolusi rendah (dpi=36) agar eksekusi cepat
                pix = page.get_pixmap(dpi=36)
                
                is_colored = False
                
                # Check warna jika colorspace bukan DeviceGray / grayscale
                if pix.colorspace and pix.colorspace.n >= 3:
                    n = pix.colorspace.n
                    # Konversi samples ke numpy array
                    data = np.frombuffer(pix.samples, dtype=np.uint8)
                    pixels = data.reshape((-1, n))
                    
                    # Ambil 3 channel pertama (RGB)
                    rgb = pixels[:, :3].astype(np.int16)
                    
                    # Hitung perbedaan antar channel warna
                    diff_rg = np.abs(rgb[:, 0] - rgb[:, 1])
                    diff_gb = np.abs(rgb[:, 1] - rgb[:, 2])
                    diff_br = np.abs(rgb[:, 2] - rgb[:, 0])
                    
                    # Jika ada piksel dengan perbedaan channel melebihi threshold,
                    # maka halaman tersebut dianggap berwarna
                    if np.any((diff_rg > threshold) | (diff_gb > threshold) | (diff_br > threshold)):
                        is_colored = True
                
                page_num = i + 1
                pages_details.append(PDFPageDetail(page=page_num, is_colored=is_colored))
                if is_colored:
                    colored_pages.append(page_num)
                else:
                    grayscale_pages.append(page_num)
                    
            # Ambil metadata
            doc_metadata = doc.metadata or {}
            metadata_clean = PDFMetadata(
                title=doc_metadata.get("title") or "",
                author=doc_metadata.get("author") or "",
                subject=doc_metadata.get("subject") or "",
                keywords=doc_metadata.get("keywords") or "",
                creator=doc_metadata.get("creator") or "",
                producer=doc_metadata.get("producer") or "",
                creationDate=doc_metadata.get("creationDate") or "",
                modDate=doc_metadata.get("modDate") or "",
                format=doc_metadata.get("format") or "",
                encryption=doc_metadata.get("encryption") or "None"
            )
            
        finally:
            doc.close()
            
        duration = time.time() - start_time
        
        return PDFColorAnalysisResponse(
            status="success",
            filename=file.filename,
            file_size_bytes=file_size,
            file_size_formatted=format_file_size(file_size),
            total_pages=total_pages,
            colored_pages_count=len(colored_pages),
            grayscale_pages_count=len(grayscale_pages),
            colored_pages=colored_pages,
            grayscale_pages=grayscale_pages,
            threshold_used=threshold,
            detection_time_seconds=round(duration, 4),
            metadata=metadata_clean,
            pages_details=pages_details
        )
        
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Terjadi kesalahan saat memproses file: {str(e)}"
        )
    finally:
        # Pastikan file temp selalu dihapus setelah proses selesai
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception as e:
                print(f"[WARNING] Gagal menghapus file sementara {temp_path}: {e}")


@router.post("/image-to-pdf")
async def convert_images_to_pdf(
    files: List[UploadFile] = File(...)
):
    valid_extensions = {".jpg", ".jpeg", ".png"}
    for file in files:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in valid_extensions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Format file tidak didukung: {file.filename}. Hanya file JPG, JPEG, dan PNG yang diperbolehkan."
            )

    doc = fitz.open()
    try:
        for file in files:
            content = await file.read()
            ext = os.path.splitext(file.filename)[1].lower().replace(".", "")
            if ext == "jpg":
                ext = "jpeg"
                
            try:
                # Buka gambar menggunakan PyMuPDF
                img_doc = fitz.open(stream=content, filetype=ext)
                pdf_bytes = img_doc.convert_to_pdf()
                img_page = fitz.open("pdf", pdf_bytes)
                doc.insert_pdf(img_page)
                img_doc.close()
                img_page.close()
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Gagal memproses gambar {file.filename}: {str(e)}"
                )

        pdf_bytes = doc.write()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": "attachment; filename=images_converted.pdf"
            }
        )
    finally:
        doc.close()
