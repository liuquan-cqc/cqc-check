import type { Directive, DirectiveBinding } from 'vue'
import { useAppStore } from '@/stores/app'

/**
 * v-permission 按钮级权限指令
 * 用法：v-permission="'reports.review'" 或 v-permission="['reports.review','reports.upload']"
 * 当前用户不具备任一所需权限时，直接移除该元素。
 */
export const vPermission: Directive = {
  mounted(el: HTMLElement, binding: DirectiveBinding<string | string[]>) {
    const app = useAppStore()
    const value = binding.value
    const permissions = Array.isArray(value) ? value : [value]
    if (!permissions.some((permission) => app.hasPermission(permission))) {
      el.parentNode?.removeChild(el)
    }
  }
}
