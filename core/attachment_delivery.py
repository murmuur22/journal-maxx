"""Authenticated callers may stream an attachment, including a single byte range."""
import re
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header


def attachment_response(request, path, attachment, inline):
    size = path.stat().st_size
    etag = f'"{attachment.checksum}"'
    start, end, partial = 0, size - 1, False
    value = request.headers.get("Range", "")
    if request.headers.get("If-Range", etag) != etag: value = ""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) < 100 else None
    if match and any(match.groups()):
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        else:
            start = max(0, size - int(last))
        if start >= size or end < start or (not first and int(last) == 0):
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            response["Accept-Ranges"] = "bytes"
            response["Cache-Control"] = "private, no-store"
            return response
        partial = True
    content_type = attachment.content_type if inline else "application/octet-stream"
    if request.method == "HEAD":
        response = HttpResponse(content_type=content_type, status=206 if partial else 200)
    elif partial:
        def chunks():
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk: break
                    remaining -= len(chunk)
                    yield chunk
        response = StreamingHttpResponse(chunks(), status=206, content_type=content_type)
    else:
        response = FileResponse(path.open("rb"), content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(not inline, attachment.original_name)
    response["Content-Length"] = str(end - start + 1 if partial else size)
    response["Accept-Ranges"] = "bytes"
    response["ETag"] = etag
    response["Cache-Control"] = "private, no-store"
    if partial: response["Content-Range"] = f"bytes {start}-{end}/{size}"
    if inline: response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response
