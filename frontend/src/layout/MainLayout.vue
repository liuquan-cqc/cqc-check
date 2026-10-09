<template>
  <el-container class="main-layout">
    <!-- 左侧深蓝色侧边栏 -->
    <el-aside :width="app.sidebarCollapsed ? '72px' : '260px'" class="sidebar" :class="{ 'is-collapsed': app.sidebarCollapsed }">
      <div class="logo-area" title="返回仪表盘" @click="$router.push('/dashboard')">
        <div class="logo-badge"><img v-if="app.systemConfig.logo_data_url" :src="app.systemConfig.logo_data_url" alt="系统 Logo" /><span v-else>CCC</span></div>
        <transition name="fade">
          <div v-show="!app.sidebarCollapsed" class="logo-text">
            <div class="logo-title">{{ app.systemConfig.system_name }}</div>
          </div>
        </transition>
      </div>

      <el-menu
        class="sidebar-menu"
        :default-active="activeMenu"
        :collapse="app.sidebarCollapsed"
        :collapse-transition="false"
        router
      >
        <el-menu-item v-for="item in visibleMenus" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <template #title>{{ item.title }}</template>
        </el-menu-item>
      </el-menu>

      <div v-show="!app.sidebarCollapsed" class="sidebar-footer">
        <div>系统版本 {{ app.systemConfig.system_version }}</div>
        <div>© {{ app.systemConfig.copyright }}</div>
      </div>
    </el-aside>

    <el-container>
      <!-- 顶栏 -->
      <el-header class="topbar" height="56px">
        <div class="topbar-left">
          <el-icon class="collapse-btn" @click="app.toggleSidebar()">
            <Expand v-if="app.sidebarCollapsed" />
            <Fold v-else />
          </el-icon>
          <span class="page-title">{{ pageTitle }}</span>
        </div>

        <div class="topbar-right">
          <!-- 深色/浅色切换 -->
          <el-tooltip :content="app.isDark ? '切换浅色模式' : '切换深色模式'" placement="bottom">
            <el-icon class="action-icon" @click="app.toggleTheme()">
              <Sunny v-if="app.isDark" />
              <Moon v-else />
            </el-icon>
          </el-tooltip>

          <!-- 用户下拉 -->
          <el-dropdown trigger="click" @command="onUserCommand">
            <div class="user-info">
              <el-avatar :size="30" class="user-avatar">{{ avatarText }}</el-avatar>
              <div class="user-meta">
                <div class="user-name">{{ auth.user?.username || '-' }}</div>
                <div class="user-role">{{ roleText }}</div>
              </div>
              <el-icon><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>{{ auth.user?.username }}（{{ roleText }}）</el-dropdown-item>
                <el-dropdown-item divided command="logout">
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- 内容区 -->
      <el-main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  Odometer,
  UploadFilled,
  Document,
  OfficeBuilding,
  Files,
  DataAnalysis,
  Setting,
  Expand,
  Fold,
  Sunny,
  Moon,
  ArrowDown,
  SwitchButton
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const app = useAppStore()

/** 菜单配置：后端权限设置控制可见性，首次加载前用内置角色避免闪空。 */
const menus = [
  { path: '/dashboard', title: '仪表盘', icon: Odometer, permission: 'dashboard.view', roles: ['admin', 'reviewer', 'viewer'] },
  { path: '/upload', title: '上传审核', icon: UploadFilled, permission: 'reports.upload', roles: ['admin', 'reviewer'] },
  { path: '/reports', title: '报告列表', icon: Document, permission: 'reports.view', roles: ['admin', 'reviewer', 'viewer'] },
  { path: '/companies', title: '企业管理', icon: OfficeBuilding, permission: 'companies.manage', roles: ['admin'] },
  { path: '/sampling', title: '下样管理', icon: Files, permission: 'sampling.manage', roles: ['admin', 'reviewer'] },
  { path: '/iteration', title: '审核迭代', icon: DataAnalysis, permission: 'iteration.manage', roles: ['admin'] },
  { path: '/settings', title: '系统设置', icon: Setting, permission: 'settings.manage', roles: ['admin'] }
]

/** 按当前角色过滤菜单 */
const visibleMenus = computed(() => menus.filter((m) => app.configLoaded ? app.hasPermission(m.permission) : auth.hasRole(...m.roles)))

/** 当前激活菜单（详情页高亮列表页） */
const activeMenu = computed(() => (route.path.startsWith('/reports') ? '/reports' : route.path))

const pageTitle = computed(() => (route.meta.title as string) || '')

const ROLE_TEXT: Record<string, string> = { admin: '管理员', reviewer: '审核员', viewer: '查看员' }
const roleText = computed(() => ROLE_TEXT[auth.user?.role || ''] || auth.user?.role || '')

/** 头像文字：取用户名首字符 */
const avatarText = computed(() => (auth.user?.username || '?').charAt(0).toUpperCase())

onMounted(async () => {
  await app.loadSystemConfig()
  const title = (route.meta.title as string) || ''
  document.title = title ? `${title} - ${app.systemConfig.system_name}` : app.systemConfig.system_name
})

/** 用户下拉命令 */
async function onUserCommand(command: string) {
  if (command === 'logout') {
    await ElMessageBox.confirm('确定退出登录吗？', '提示', { type: 'warning' })
    auth.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
.main-layout {
  height: 100%;
}

.main-layout > .el-container {
  min-width: 0;
}

/* ===== 侧边栏 ===== */
.sidebar {
  background: linear-gradient(180deg, var(--ccc-sidebar-bg) 0%, var(--ccc-sidebar-bg-deep) 100%);
  display: flex;
  flex-direction: column;
  transition: width var(--ccc-transition);
  overflow: hidden;
}

.logo-area {
  display: flex;
  align-items: center;
  gap: 13px;
  padding: 18px 16px;
  cursor: pointer;
  min-height: 84px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  transition: padding var(--ccc-transition), background-color var(--ccc-transition);
}

.logo-area:hover {
  background: rgba(255, 255, 255, 0.035);
}

.sidebar.is-collapsed .logo-area {
  justify-content: center;
  padding-inline: 0;
}

.logo-badge {
  width: 48px;
  height: 48px;
  padding: 6px;
  border-radius: 13px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  background: rgba(255, 255, 255, 0.96);
  color: var(--ccc-primary);
  font-weight: 700;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-sizing: border-box;
  box-shadow: 0 8px 22px rgba(5, 20, 43, 0.24), inset 0 0 0 1px rgba(255, 255, 255, 0.55);
}

.logo-badge img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: 7px;
}

.logo-text {
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
}

.logo-title {
  font-size: 17px;
  font-weight: 650;
  line-height: 1.42;
  letter-spacing: 0.015em;
  max-width: 174px;
  white-space: normal;
  text-wrap: balance;
  text-shadow: 0 1px 10px rgba(0, 0, 0, 0.16);
}

.sidebar-menu {
  flex: 1;
  border-right: none;
  background: transparent;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: rgba(255, 255, 255, 0.75);
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.08);
  --el-menu-active-color: #fff;
}

.sidebar-menu :deep(.el-menu-item) {
  margin: 2px 8px;
  border-radius: 8px;
}

.sidebar-menu :deep(.el-menu-item.is-active) {
  background: var(--ccc-primary);
  color: #fff;
}

.sidebar-footer {
  padding: 14px;
  color: rgba(255, 255, 255, 0.4);
  font-size: 12px;
  white-space: nowrap;
}

.sidebar-footer > div + div {
  margin-top: 5px;
}

/* ===== 顶栏 ===== */
.topbar {
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-lighter);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 14px;
}

.collapse-btn {
  font-size: 18px;
  cursor: pointer;
  color: var(--el-text-color-regular);
  transition: color var(--ccc-transition);
}

.collapse-btn:hover {
  color: var(--ccc-primary);
}

.page-title {
  font-size: 16px;
  font-weight: 600;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 18px;
}

.action-icon {
  font-size: 18px;
  cursor: pointer;
  color: var(--el-text-color-regular);
  transition: color var(--ccc-transition);
}

.action-icon:hover {
  color: var(--ccc-primary);
}

.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
}

.user-avatar {
  background: var(--ccc-primary);
  font-size: 14px;
}

.user-meta {
  line-height: 1.2;
}

.user-name {
  font-size: 13px;
  font-weight: 500;
}

.user-role {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

/* ===== 内容区 ===== */
.content {
  background: var(--ccc-content-bg);
  padding: 20px;
  overflow-y: auto;
}

/* 页面切换过渡 */
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* 平板适配：缩小内容区内边距 */
@media (max-width: 1024px) {
  .content {
    padding: 14px;
  }

  .user-meta {
    display: none;
  }
}

/* 手机端固定使用紧凑侧栏，避免展开状态挤压业务表单。 */
@media (max-width: 640px) {
  .sidebar {
    width: 64px !important;
    flex: 0 0 64px;
  }

  .sidebar .logo-area {
    justify-content: center;
    min-height: 64px;
    padding: 8px 0;
  }

  .sidebar .logo-badge {
    width: 42px;
    height: 42px;
  }

  .sidebar .logo-text,
  .sidebar .sidebar-footer,
  .sidebar-menu :deep(.el-menu-item > span) {
    display: none !important;
  }

  .sidebar-menu :deep(.el-menu-item) {
    justify-content: center;
    padding: 0 !important;
    font-size: 0;
  }

  .sidebar-menu :deep(.el-menu-item .el-icon) {
    margin-right: 0;
    font-size: 18px;
  }

  .topbar {
    padding: 0 10px;
  }

  .topbar-left,
  .topbar-right {
    gap: 9px;
  }

  .page-title,
  .user-info > .el-icon {
    display: none;
  }

  .content {
    padding: 10px;
  }
}
</style>
