import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** The assistant's text is untrusted: no raw HTML is rendered (react-markdown's default), and links open in a new tab without handing over the opener. */
export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer nofollow">{children}</a>,
          // an image would be fetched the moment it is shown, by whatever address the text names: that is a way to leak data, so it becomes a link the reader may open
          img: ({ src, alt }) => <a href={typeof src === "string" ? src : undefined} target="_blank" rel="noopener noreferrer nofollow">{alt || "image"}</a>,
        }}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
