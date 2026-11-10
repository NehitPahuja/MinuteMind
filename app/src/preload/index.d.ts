import type { MinuteMindApi } from '../shared/api'

declare global {
  interface Window {
    api: MinuteMindApi
  }
}
