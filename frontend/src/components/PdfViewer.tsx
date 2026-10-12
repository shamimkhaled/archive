import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";

GlobalWorkerOptions.workerSrc = workerUrl;

const ASSETS = `${import.meta.env.BASE_URL}pdfjs/`;
const ZOOMS = [0.75, 1, 1.25, 1.5, 2];

interface Props {
  url: string;
  page: number;
  onPageChange: (page: number) => void;
  onPageCount?: (count: number) => void;
}

/**
 * Renders one page at a time from the server's range-capable PDF route, so a 300-page scan
 * downloads only the chunks for the pages read. The PDF is already watermarked server-side.
 */
export function PdfViewer({ url, page, onPageChange, onPageCount }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1.25);

  useEffect(() => {
    setPdf(null);
    setError(null);
    const task = getDocument({
      url,
      withCredentials: true,
      rangeChunkSize: 256 * 1024,
      disableAutoFetch: true,
      cMapUrl: `${ASSETS}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${ASSETS}standard_fonts/`,
      wasmUrl: `${ASSETS}wasm/`,
    });
    task.promise.then(
      (doc) => {
        setPdf(doc);
        onPageCount?.(doc.numPages);
      },
      (reason: unknown) => setError(reason instanceof Error ? reason.message : "Could not open the document"),
    );
    return () => {
      void task.destroy();
    };
  }, [url]);

  useEffect(() => {
    if (!pdf || !canvas.current) return;
    let render: RenderTask | null = null;
    let cancelled = false;
    const target = Math.min(Math.max(page, 1), pdf.numPages);
    void pdf.getPage(target).then((pdfPage) => {
      if (cancelled || !canvas.current) return;
      const ratio = window.devicePixelRatio || 1;
      const viewport = pdfPage.getViewport({ scale: zoom * ratio });
      const element = canvas.current;
      element.width = Math.floor(viewport.width);
      element.height = Math.floor(viewport.height);
      element.style.width = `${Math.floor(viewport.width / ratio)}px`;
      render = pdfPage.render({ canvas: element, viewport });
      render.promise.catch(() => undefined);
    });
    return () => {
      cancelled = true;
      render?.cancel();
    };
  }, [pdf, page, zoom]);

  if (error) return <p className="error">{error}</p>;
  const count = pdf?.numPages ?? 0;
  return (
    <div className="pdf">
      <div className="pdf-toolbar">
        <button type="button" disabled={page <= 1} onClick={() => onPageChange(page - 1)} aria-label="Previous page">
          ‹
        </button>
        <span>
          Page{" "}
          <input
            type="number"
            min={1}
            max={count || undefined}
            value={page}
            onChange={(event) => {
              const next = Number(event.target.value);
              if (next >= 1 && (!count || next <= count)) onPageChange(next);
            }}
            aria-label="Page number"
          />{" "}
          of {count || "…"}
        </span>
        <button type="button" disabled={!count || page >= count} onClick={() => onPageChange(page + 1)} aria-label="Next page">
          ›
        </button>
        <select value={zoom} onChange={(event) => setZoom(Number(event.target.value))} aria-label="Zoom">
          {ZOOMS.map((z) => (
            <option key={z} value={z}>
              {Math.round(z * 100)}%
            </option>
          ))}
        </select>
      </div>
      <div className="pdf-stage">{pdf ? <canvas ref={canvas} /> : <p className="muted">Opening document…</p>}</div>
    </div>
  );
}
