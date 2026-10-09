import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'

/**
 * 路由表
 * meta.title：页面标题（顶栏展示 + document.title）
 * meta.roles：允许访问的角色；缺省表示登录即可访问
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '登录', public: true }
  },
  {
    path: '/',
    component: () => import('@/layout/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '仪表盘', permission: 'dashboard.view' }
      },
      {
        path: 'upload',
        name: 'Upload',
        component: () => import('@/views/Upload.vue'),
        meta: { title: '上传审核', permission: 'reports.upload' }
      },
      {
        path: 'reports',
        name: 'Reports',
        component: () => import('@/views/Reports.vue'),
        meta: { title: '报告列表', permission: 'reports.view' }
      },
      {
        path: 'reports/:id',
        name: 'ReportDetail',
        component: () => import('@/views/ReportDetail.vue'),
        meta: { title: '审核结果', permission: 'reports.view' }
      },
      {
        path: 'companies',
        name: 'Companies',
        component: () => import('@/views/Companies.vue'),
        meta: { title: '企业管理', permission: 'companies.manage' }
      },
      {
        path: 'sampling',
        name: 'Sampling',
        component: () => import('@/views/Sampling.vue'),
        meta: { title: '下样管理', permission: 'sampling.manage' }
      },
      {
        path: 'iteration',
        name: 'IterationCenter',
        component: () => import('@/views/IterationCenter.vue'),
        meta: { title: '审核迭代中心', permission: 'iteration.manage' }
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/Settings.vue'),
        meta: { title: '系统设置', permission: 'settings.manage' }
      },
      {
        path: 'no-access',
        name: 'NoAccess',
        component: () => import('@/views/NoAccess.vue'),
        meta: { title: '暂无权限' }
      }
    ]
  },
  // 兜底：未匹配路径重定向首页
  { path: '/:pathMatch(.*)*', redirect: '/' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 全局前置守卫：登录校验 + 角色校验
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const app = useAppStore()
  const title = (to.meta.title as string) || ''
  document.title = title ? `${title} - ${app.systemConfig.system_name}` : app.systemConfig.system_name

  if (to.meta.public) {
    // 已登录用户访问登录页 → 回首页
    return auth.isLoggedIn ? '/' : true
  }
  if (!auth.isLoggedIn) {
    // 未登录 → 跳登录页并记录来源地址
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (!app.configLoaded) await app.loadSystemConfig()
  const permission = to.meta.permission as string | undefined
  if (permission && !app.hasPermission(permission)) {
    // 无权限时进入该账号实际可访问的第一个页面，避免只开放部分权限后循环跳转。
    ElMessage.warning('无权限访问该页面')
    const candidates = [
      ['dashboard.view', '/dashboard'], ['reports.upload', '/upload'], ['reports.view', '/reports'],
      ['companies.manage', '/companies'], ['sampling.manage', '/sampling'], ['iteration.manage', '/iteration'], ['settings.manage', '/settings']
    ]
    return candidates.find(([required]) => app.hasPermission(required))?.[1] || '/no-access'
  }
  return true
})

export default router
