"""Text chunking for RAG — split text into overlapping chunks."""


def chunk_text(text: str, max_size: int = 500, overlap: int = 100) -> list[str]:
    """Split text into chunks with sliding overlap for semantic continuity."""
    if overlap >= max_size:
        raise ValueError(f"overlap={overlap} must be < max_size={max_size}")

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer: list[str] = []

    for p in paragraphs:
        if len(p) > max_size:
            _flush_buffer(chunks, buffer)
            buffer = []
            _split_long_paragraph(chunks, p, max_size, overlap)
            continue

        projected = "\n\n".join([*buffer, p])
        if len(projected) > max_size and buffer:
            _flush_buffer(chunks, buffer)
            buffer = _take_overlap(buffer, overlap)
        buffer.append(p)

    _flush_buffer(chunks, buffer)
    return chunks


def _flush_buffer(chunks: list[str], buffer: list[str]) -> None:
    if buffer:
        chunks.append("\n\n".join(buffer).strip())


def _take_overlap(buffer: list[str], overlap: int) -> list[str]:
    if overlap <= 0 or not buffer:
        return []
    joined = "\n\n".join(buffer)
    return [joined[-overlap:]]


def _split_long_paragraph(chunks: list[str], paragraph: str, max_size: int, overlap: int) -> None:
    words = paragraph.split()
    word_buf: list[str] = []
    for w in words:
        if len(" ".join([*word_buf, w])) > max_size and word_buf:
            chunks.append(" ".join(word_buf).strip())
            word_buf = _get_word_overlap(word_buf, overlap, w)
        else:
            word_buf.append(w)
    if word_buf:
        chunks.append(" ".join(word_buf).strip())


def _get_word_overlap(word_buf: list[str], overlap: int, current_word: str) -> list[str]:
    if not overlap:
        return [current_word]
    num_words = max(1, overlap // 8)
    return [*word_buf[-num_words:], current_word]
