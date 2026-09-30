/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface ImportMetaEnv {
  readonly VITE_GITHUB_OAUTH_CLIENT_ID: string
  readonly VITE_GITHUB_OAUTH_REDIRECT_URI: string
  readonly VITE_GITEE_OAUTH_CLIENT_ID: string
  readonly VITE_GITEE_OAUTH_REDIRECT_URI: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
