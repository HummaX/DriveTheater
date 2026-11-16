import { isValidElement, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { buttonMetadata } from './buttonMetadata';

export const TOOLTIP_DELAY_MS = 500;

function textContent(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textContent).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return textContent(node.props.children);
  return '';
}

type ActionButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  hint?: string;
  action?: string;
  context?: string;
  tooltip?: boolean;
};

/** Local metadata on every action; delayed visual help only where useful. */
export function ActionButton({ hint: explicitHint, action: explicitAction, context, tooltip, children, onFocus, onBlur, onPointerEnter, onPointerLeave, onPointerDown, onClick, onKeyDown, ...props }: ActionButtonProps) {
  const visibleLabel = textContent(children).trim();
  const defaults = buttonMetadata(props['aria-label'] ?? visibleLabel);
  const hint = explicitHint ?? defaults.hint;
  const action = explicitAction ?? defaults.action;
  const showTooltip = tooltip ?? !visibleLabel;
  const tooltipId = useId();
  const [anchor, setAnchor] = useState<{ left: number; top: number; width: number; above: boolean } | null>(null);
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const focused = useRef(false);
  const hovered = useRef(false);
  const cancelOpen = () => { if (openTimer.current !== null) clearTimeout(openTimer.current); openTimer.current = null; };
  const cancelClose = () => { if (closeTimer.current !== null) clearTimeout(closeTimer.current); closeTimer.current = null; };
  const close = () => { cancelOpen(); cancelClose(); setAnchor(null); };
  const scheduleClose = () => { if (focused.current || hovered.current) return; cancelOpen(); cancelClose(); closeTimer.current = setTimeout(() => setAnchor(null), 150); };
  const open = (button: HTMLButtonElement) => {
    if (!showTooltip) return;
    cancelClose(); cancelOpen();
    openTimer.current = setTimeout(() => {
      openTimer.current = null;
      if (!button.isConnected) return;
      const rect = button.getBoundingClientRect();
      const width = Math.min(300, window.innerWidth - 24);
      const above = window.innerHeight - rect.bottom < 130 && rect.top > 130;
      setAnchor({ left: Math.max(12, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 12)), top: above ? rect.top - 8 : rect.bottom + 8, width, above });
    }, TOOLTIP_DELAY_MS);
  };

  useEffect(() => () => {
    if (openTimer.current !== null) clearTimeout(openTimer.current);
    if (closeTimer.current !== null) clearTimeout(closeTimer.current);
  }, []);
  useEffect(() => {
    if (!anchor) return;
    const dismiss = () => setAnchor(null);
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') dismiss(); };
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', dismiss, true);
    document.addEventListener('keydown', escape);
    return () => {
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('scroll', dismiss, true);
      document.removeEventListener('keydown', escape);
    };
  }, [anchor]);

  return <>
    <button {...props} data-action={action} data-context={context} data-hint={hint} data-tooltip={showTooltip ? 'true' : undefined} aria-description={hint} aria-describedby={anchor ? [props['aria-describedby'], tooltipId].filter(Boolean).join(' ') : props['aria-describedby']}
      onPointerEnter={(event) => { hovered.current = event.pointerType !== 'touch'; onPointerEnter?.(event); if (hovered.current) open(event.currentTarget); }}
      onPointerLeave={(event) => { hovered.current = false; onPointerLeave?.(event); scheduleClose(); }}
      onPointerDown={(event) => { close(); onPointerDown?.(event); }}
      onFocus={(event) => { focused.current = event.currentTarget.matches(':focus-visible'); onFocus?.(event); if (focused.current) open(event.currentTarget); }}
      onBlur={(event) => { focused.current = false; scheduleClose(); onBlur?.(event); }}
      onKeyDown={(event) => { if (event.key === 'Escape') close(); onKeyDown?.(event); }}
      onClick={(event) => { close(); onClick?.(event); }}>{children}</button>
    {anchor && createPortal(<span id={tooltipId} role="tooltip" className="action-tooltip" style={{ left: anchor.left, top: anchor.top, width: anchor.width, transform: anchor.above ? 'translateY(-100%)' : undefined }} onPointerEnter={() => { hovered.current = true; cancelClose(); }} onPointerLeave={() => { hovered.current = false; scheduleClose(); }}>{hint}</span>, document.body)}
  </>;
}
