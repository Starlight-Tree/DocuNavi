import { defineCloudflareConfig } from '@opennextjs/cloudflare';

// Cloudflare Workers（Free プラン）で動かすための OpenNext の設定。画面は静的な書き出しが中心で、キャッシュの置き場は使わない。
export default defineCloudflareConfig();
