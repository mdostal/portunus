import type { ReactNode } from "react";

// Small, narrow Markdown -> React renderer for generate_report()'s exact
// output shape (design-discussion.md §4, portunus-leak-visibility Story
// 03) -- #/##/### headers, top-level `- ` bullets with one level of
// `  - ` indented sub-bullets, **bold**, and inline `code`. Deliberately
// NOT a general-purpose markdown parser: ui/package.json has exactly 3
// runtime dependencies, and generate_report()'s output is fully
// controlled and this narrow -- a library is unjustified weight for
// content this predictable. An unrecognized line renders as plain text
// rather than being dropped, so a future generate_report() change
// degrades gracefully instead of silently losing content.

interface ListItem {
  text: string;
  subItems: string[];
}

type Block =
  | { kind: "h1" | "h2" | "h3" | "p"; text: string }
  | { kind: "ul"; items: ListItem[] };

function parseBlocks(markdown: string): Block[] {
  const blocks: Block[] = [];
  let currentList: ListItem[] | null = null;

  function flush() {
    if (currentList && currentList.length > 0) {
      blocks.push({ kind: "ul", items: currentList });
    }
    currentList = null;
  }

  for (const line of markdown.split("\n")) {
    if (line.startsWith("### ")) {
      flush();
      blocks.push({ kind: "h3", text: line.slice(4) });
    } else if (line.startsWith("## ")) {
      flush();
      blocks.push({ kind: "h2", text: line.slice(3) });
    } else if (line.startsWith("# ")) {
      flush();
      blocks.push({ kind: "h1", text: line.slice(2) });
    } else if (line.startsWith("  - ")) {
      if (!currentList) currentList = [];
      if (currentList.length === 0) currentList.push({ text: "", subItems: [] });
      currentList[currentList.length - 1].subItems.push(line.slice(4));
    } else if (line.startsWith("- ")) {
      if (!currentList) currentList = [];
      currentList.push({ text: line.slice(2), subItems: [] });
    } else if (line.trim() === "") {
      flush();
    } else {
      flush();
      blocks.push({ kind: "p", text: line });
    }
  }
  flush();
  return blocks;
}

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index));
    const token = match[0];
    if (token.startsWith("**")) {
      nodes.push(<strong key={`${keyPrefix}-${i++}`}>{token.slice(2, -2)}</strong>);
    } else {
      nodes.push(<code key={`${keyPrefix}-${i++}`}>{token.slice(1, -1)}</code>);
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

export function renderReportMarkdown(markdown: string): ReactNode {
  const blocks = parseBlocks(markdown);
  return (
    <div className="report-view">
      {blocks.map((b, i) => {
        switch (b.kind) {
          case "h1":
            return <h1 key={i}>{renderInline(b.text, `h1-${i}`)}</h1>;
          case "h2":
            return <h2 key={i}>{renderInline(b.text, `h2-${i}`)}</h2>;
          case "h3":
            return <h3 key={i}>{renderInline(b.text, `h3-${i}`)}</h3>;
          case "p":
            return <p key={i}>{renderInline(b.text, `p-${i}`)}</p>;
        }
        return (
          <ul key={i}>
            {b.items.map((item, j) => (
              <li key={j}>
                {renderInline(item.text, `li-${i}-${j}`)}
                {item.subItems.length > 0 && (
                  <ul className="report-sublist">
                    {item.subItems.map((sub, k) => (
                      <li key={k}>{renderInline(sub, `sli-${i}-${j}-${k}`)}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        );
      })}
    </div>
  );
}
