import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NotificationInbox, Notifications, type NotificationItem } from './Notifications';

const item: NotificationItem = { id: 'notice', kind: 'source_failure', title: 'Source needs attention', message: 'Reconnect this source.', entity_id: 'source', created_at: 1700000000, read_at: null, read: false };

afterEach(() => vi.unstubAllGlobals());

describe('notification controls', () => {
  it('exports the requested refreshKey component and renders without sending tests', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    const html = renderToStaticMarkup(createElement(Notifications, { refreshKey: 1 }));
    expect(html).toContain('Notifications');
    expect(html).toContain('External notification channels');
    expect(html).toContain('not configured');
    expect(html).toContain('Save channel');
    expect(html).toContain('Enable external delivery when saved');
    expect(html).toContain('Current password');
    expect(html).toContain('Authenticator code');
    expect(html).toContain('Or one recovery code');
    expect(html).toContain('Saved credentials are encrypted and never shown again');
    expect(html).not.toContain('Queue test');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('starts disabled and never embeds saved credentials or browser storage', () => {
    const html = renderToStaticMarkup(createElement(Notifications));
    expect(html).not.toContain('checked=""');
    expect(html).toContain('type="password"');
    expect(html).toContain('value=""');
    expect(html).not.toContain('localStorage');
    expect(html).not.toContain('sessionStorage');
    expect(html).not.toContain('http://');
  });

  it('shows unread read/dismiss actions with existing accessible ActionButton metadata', () => {
    const html = renderToStaticMarkup(createElement(NotificationInbox, { items: [item], busy: false, onRead: vi.fn(), onDismiss: vi.fn() }));
    expect(html).toContain('Unread: ');
    expect(html).toContain('Mark read');
    expect(html).toContain('Dismiss');
    expect(html).toContain('aria-description=');
    expect(html).toContain('<time');
    expect(html).toContain('Reconnect this source.');
    const readHtml = renderToStaticMarkup(createElement(NotificationInbox, { items: [{ ...item, read: true, read_at: 1700000010 }], busy: true, onRead: vi.fn(), onDismiss: vi.fn() }));
    expect(readHtml).not.toContain('Mark read');
    expect(readHtml).not.toContain('Unread: ');
    expect(readHtml).toContain('disabled=""');
  });

  it('escapes notification content rather than rendering HTML or external links', () => {
    const html = renderToStaticMarkup(createElement(NotificationInbox, { items: [{ ...item, title: '<script>bad()</script>', message: '<img src=x onerror=bad()>' }], busy: false, onRead: vi.fn(), onDismiss: vi.fn() }));
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<a ');
  });

  it('gives useful empty-state direction without an external send action', () => {
    const html = renderToStaticMarkup(createElement(NotificationInbox, { items: [], busy: false, onRead: vi.fn(), onDismiss: vi.fn() }));
    expect(html).toContain('No notifications yet');
    expect(html).toContain('Source failures, recoveries and account events');
    expect(html).not.toContain('<button');
  });
});
