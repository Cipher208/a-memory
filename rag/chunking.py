"""Text chunking for RAG — split text into overlapping chunks."""


def chunk_text(text: str, max_size: int = 500, overlap: int = 100) -> list[str]:
    """Split text into chunks with sliding overlap for semantic continuity.

    Rules:
      1. Split on double newline (paragraph).
      2. When accumulated buffer reaches max_size, flush it.
         Last `overlap` chars carry over to next chunk.
      3. Paragraphs longer than max_size are split by words
         (overlap only at paragraph boundaries, not within).
    """
    if overlap >= max_size:
        raise ValueError("overlap=%d must be < max_size=%d" % (overlap, max_size))

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer: list[str] = []

    def _flush(buf: list[str]) -> None:
        if not buf:
            return
        chunks.append("\n\n".join(buf).strip())

    def _take_overlap(buf: list[str], n: int) -> list[str]:
        """Return last n chars of joined buffer as leading part for next chunk."""
        if n <= 0 or not buf:
            return []
        joined = "\n\n".join(buf)
        tail = joined[-n:]
        return [tail]

    for p in paragraphs:
        if len(p) > max_size:
            # Flush current buffer first
            _flush(buffer)
            buffer = []
            # Split long paragraph by words
            words = p.split()
            word_buf: list[str] = []
            for w in words:
                if len(" ".join([*word_buf, w])) > max_size and word_buf:
                    chunks.append(" ".join(word_buf).strip())
                    # Word-level overlap: keep last N words
                    word_buf = [*word_buf[-max(1, overlap // 8) :], w] if overlap else [w]
                else:
                    word_buf.append(w)
            if word_buf:
                chunks.append(" ".join(word_buf).strip())
            continue

        projected = "\n\n".join([*buffer, p])
        if len(projected) > max_size and buffer:
            _flush(buffer)
            buffer = _take_overlap(buffer, overlap)
        buffer.append(p)

    _flush(buffer)
    return chunks
