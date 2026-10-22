"""Parse a bounded administrator upload without trusting file names or HTTP lengths."""

from collections.abc import AsyncIterator

from fastapi import Request
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from src.core.infra.exceptions import WebImportError
from src.services.imports.records import SCHEMAS

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = 26 * 1024 * 1024


async def bounded_body(request: Request) -> AsyncIterator[bytes]:
    """Enforce the complete multipart byte limit, including streams without Content-Length."""
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            raise WebImportError(413, "Общий размер загрузки превышает 26 МиБ")
        yield chunk


async def read_upload(request: Request) -> dict[str, bytes]:
    """Accept only expected CSV names after authorization; always close partial spool files.

    File bytes are never decoded or normalized here. Missing expected files are left to
    the shared preflight so that a structurally incomplete dataset gets a failed journal
    attempt. Transport errors (names, duplicates, limits, fields) never start an import.
    Starlette 0.50's max_part_size limits fields, so actual file sizes are checked as well.
    """
    if (
        request.headers.get("content-type", "").partition(";")[0].strip().lower()
        != "multipart/form-data"
    ):
        raise WebImportError(415, "Выберите CSV-файлы и отправьте multipart/form-data")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
            if size < 0:
                raise ValueError
        except ValueError:
            raise WebImportError(422, "Некорректный размер загрузки") from None
        if size > MAX_BODY_BYTES:
            raise WebImportError(413, "Общий размер загрузки превышает 26 МиБ")
    parser = MultiPartParser(
        request.headers,
        bounded_body(request),
        max_files=len(SCHEMAS),
        max_fields=0,
        max_part_size=1024,
    )
    try:
        form = await parser.parse()
        result: dict[str, bytes] = {}
        for field, upload in form.multi_items():
            if field != "files" or not isinstance(upload, UploadFile):
                raise WebImportError(422, "Разрешены только файлы в поле files")
            disposition = upload.headers.get("content-disposition", "")
            if "/" in disposition or "\\" in disposition:
                raise WebImportError(422, "Передавайте имена CSV без каталогов и путей")
            name = upload.filename
            if name not in SCHEMAS:
                raise WebImportError(422, "Имя файла должно совпадать с одним из пяти CSV набора")
            if name in result:
                raise WebImportError(422, "Один CSV-файл выбран несколько раз")
            if upload.size is not None and upload.size > MAX_FILE_BYTES:
                raise WebImportError(413, "Размер одного CSV превышает 5 МиБ")
            content = await upload.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise WebImportError(413, "Размер одного CSV превышает 5 МиБ")
            result[name] = content
        if not result:
            raise WebImportError(422, "Выберите полный набор из пяти CSV-файлов")
        return result
    except (MultiPartException, MultipartParseError, UnicodeError, LookupError):
        raise WebImportError(
            422, "Некорректная форма загрузки: нужны только пять CSV-файлов"
        ) from None
    finally:
        # The pinned parser keeps partially received files outside FormData until part_end.
        # Its own cleanup only handles MultiPartException, not disconnect/cancellation.
        for file in parser._files_to_close_on_error:
            file.close()
