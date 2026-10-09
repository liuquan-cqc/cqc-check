<template>
  <div class="iteration-page">
    <section class="leadership-hero ccc-card" v-loading="loading">
      <div class="hero-main">
        <div class="hero-status"><span class="status-dot"></span>审核系统运行正常</div>
        <h1>审核质量治理看板</h1>
        <p>将人工发现的审核问题沉淀为可验证、可发布、可回退的审核能力。</p>
      </div>
      <div class="hero-release" v-if="summary.active_version">
        <span>当前正式版本</span>
        <strong>V{{ summary.active_version.version_no }}</strong>
        <p>{{ summary.active_version.name }}</p>
        <small>发布于 {{ formatTime(summary.active_version.published_at) }}</small>
      </div>
    </section>

    <section class="executive-metrics">
      <article class="metric-card metric-primary">
        <span>当前版本</span>
        <strong>{{ summary.active_version ? `V${summary.active_version.version_no}` : '基础版' }}</strong>
        <small>{{ activeRuleCount }} 条已发布规则正在生效</small>
      </article>
      <article class="metric-card">
        <span>最新回归验证</span>
        <strong>{{ regressionSummary }}</strong>
        <small>{{ regressionStatusText }}</small>
      </article>
      <article class="metric-card">
        <span>已完成纠错闭环</span>
        <strong>{{ approvedCorrectionCount }}</strong>
        <small>已确认并留存正确处理方式</small>
      </article>
      <article class="metric-card" :class="{ 'metric-warning': openRiskCount > 0 }">
        <span>当前待处理风险</span>
        <strong>{{ openRiskCount }}</strong>
        <small>{{ openRiskCount ? '需管理员处理' : '无发布阻断项' }}</small>
      </article>
    </section>

    <section class="governance-grid">
      <article class="release-story ccc-card">
        <div class="section-heading">
          <div><span>本次迭代成果</span><h2>{{ currentVersionTitle }}</h2></div>
          <el-tag type="success" effect="plain">已发布</el-tag>
        </div>
        <p class="release-description">{{ currentVersionDescription }}</p>
        <div class="release-facts">
          <div><strong>{{ activeRuleCount }}</strong><span>正式规则</span></div>
          <div><strong>{{ summary.regression_cases || 0 }}</strong><span>回归样本</span></div>
          <div><strong>{{ latestRegression?.failed || 0 }}</strong><span>未通过项</span></div>
        </div>
        <div class="version-components" v-if="summary.component_versions">
          <span>审核快照 {{ summary.component_versions.iteration_version }}</span>
          <span>规则库 {{ summary.component_versions.structured_rulebase_version }}</span>
          <span>依据库 {{ summary.component_versions.knowledge_version }}</span>
        </div>
      </article>

      <article class="health-panel ccc-card">
        <div class="section-heading compact"><div><span>风险与健康度</span><h2>正式审核受控</h2></div></div>
        <ul class="health-list">
          <li><span class="health-icon ok"><el-icon><CircleCheck /></el-icon></span><div><strong>回归校验</strong><small>{{ regressionStatusText }}</small></div><b>{{ latestRegression?.failed ? '需关注' : '正常' }}</b></li>
          <li><span class="health-icon" :class="summary.pending_corrections ? 'warn' : 'ok'"><el-icon><EditPen /></el-icon></span><div><strong>纠错待办</strong><small>{{ summary.pending_corrections || 0 }} 条待管理员确认</small></div><b>{{ summary.pending_corrections ? '待处理' : '正常' }}</b></li>
          <li><span class="health-icon" :class="summary.draft_rules ? 'warn' : 'ok'"><el-icon><DocumentChecked /></el-icon></span><div><strong>规则发布</strong><small>{{ summary.draft_rules || 0 }} 条草稿尚未进入正式版本</small></div><b>{{ summary.draft_rules ? '待评估' : '正常' }}</b></li>
        </ul>
      </article>
    </section>

    <section class="closed-loop ccc-card">
      <div class="section-heading compact"><div><span>持续改进机制</span><h2>问题从发现到上线的闭环</h2></div><small>只有通过人工确认和回归校验的规则才会进入正式审核</small></div>
      <div class="loop-steps">
        <div><i>1</i><span>发现问题</span><strong>{{ allCorrections.length }}</strong><small>已留存纠错</small></div>
        <div><i>2</i><span>人工确认</span><strong>{{ approvedCorrectionCount }}</strong><small>已确认</small></div>
        <div><i>3</i><span>形成规则</span><strong>{{ activeRuleCount }}</strong><small>已发布</small></div>
        <div><i>4</i><span>回归验证</span><strong>{{ regressionSummary }}</strong><small>最新结果</small></div>
        <div><i>5</i><span>正式上线</span><strong>{{ summary.active_version ? `V${summary.active_version.version_no}` : '-' }}</strong><small>当前版本</small></div>
      </div>
    </section>

    <section class="rule-atlas ccc-card" v-loading="rulebaseTreeLoading">
      <div class="atlas-heading">
        <div>
          <span>规则资产全景</span>
          <h2>审核规则知识图谱</h2>
          <p>按材料体系、标准系列、产品型号和试验项目逐层查看规则，所有节点均来自当前启用的结构化规则库。</p>
        </div>
        <div class="atlas-actions">
          <el-button :icon="Refresh" @click="resetRuleGraph">重置展开</el-button>
          <el-button type="primary" :icon="FullScreen" @click="atlasDialogVisible = true">全屏展示</el-button>
        </div>
      </div>
      <div class="atlas-toolbar">
        <el-input v-model="atlasSearch" :prefix-icon="Search" clearable placeholder="搜索型号、标准、试验项目或规则" class="atlas-search" />
        <div class="atlas-filters" aria-label="图谱类型筛选">
          <button v-for="item in atlasFilterOptions" :key="item.value" type="button" :class="{ active: atlasFilter === item.value }" @click="atlasFilter = item.value">{{ item.label }}</button>
        </div>
      </div>
      <div class="atlas-layout">
        <aside class="atlas-overview" v-if="rulebaseTreeData.summary">
          <div class="overview-title"><strong>规则库概览</strong><span>实时数据库统计</span></div>
          <div class="overview-stat"><i class="stat-blue"><el-icon><Collection /></el-icon></i><span>标准体系<strong>{{ rulebaseTreeData.summary.series || 0 }}</strong></span></div>
          <div class="overview-stat"><i class="stat-cyan"><el-icon><Document /></el-icon></i><span>产品/方法标准<strong>{{ rulebaseTreeData.summary.standards || 0 }}</strong></span></div>
          <div class="overview-stat"><i class="stat-green"><el-icon><Files /></el-icon></i><span>产品型号<strong>{{ rulebaseTreeData.summary.models || 0 }}</strong></span></div>
          <div class="overview-stat"><i class="stat-orange"><el-icon><DataAnalysis /></el-icon></i><span>试验项目<strong>{{ rulebaseTreeData.summary.test_items || 0 }}</strong></span></div>
          <div class="overview-stat overview-total"><i><el-icon><CircleCheck /></el-icon></i><span>启用结构化规则<strong>{{ rulebaseTreeData.summary.total_rules || 0 }}</strong></span></div>
          <div class="overview-version"><span>当前规则库</span><b>{{ rulebaseTreeData.summary.version || '-' }}</b></div>
        </aside>
        <div class="atlas-canvas">
          <VChart v-if="filteredRuleTree" :key="ruleGraphKey" :option="ruleTreeOption" height="610px" @node-click="selectRuleNode" />
          <el-empty v-else description="没有找到匹配的规则节点" :image-size="80" />
          <div class="atlas-legend"><span><i class="legend-root"></i>规则总览</span><span><i class="legend-standard"></i>标准与型号</span><span><i class="legend-test"></i>试验项目</span><span><i class="legend-matrix"></i>型号适用</span><span><i class="legend-parameter"></i>参数规则</span><small>滚轮缩放，拖动画布，点击节点展开</small></div>
        </div>
        <aside class="atlas-detail">
          <div class="detail-heading"><span>节点信息</span><b>{{ ruleNodeTypeText(selectedRuleNode?.type) }}</b></div>
          <h3>{{ selectedRuleNode?.name || '点击图谱节点' }}</h3>
          <p v-if="selectedRuleNode?.count">下辖 <b>{{ selectedRuleNode.count }}</b> 条最终规则</p>
          <dl v-if="selectedRuleMeta.length">
            <template v-for="entry in selectedRuleMeta" :key="entry[0]">
              <dt>{{ ruleMetaLabel(entry[0]) }}</dt><dd>{{ entry[1] || '-' }}</dd>
            </template>
          </dl>
          <div v-else class="detail-hint">点击图谱中的节点，可查看类型、适用条件、标准依据和验证状态。</div>
        </aside>
      </div>
    </section>

    <section class="version-journey ccc-card">
      <div class="section-heading compact"><div><span>近期改进</span><h2>系统正在解决哪些审核问题</h2></div><el-button link type="primary" @click="activeTab = 'versions'">查看全部版本</el-button></div>
      <div class="journey-list">
        <article v-for="version in recentVersions" :key="version.id" :class="{ current: version.status === 'active' }">
          <div class="journey-version">V{{ version.version_no }}</div>
          <div><strong>{{ version.name }}</strong><p>{{ version.description || '该版本未填写迭代说明。' }}</p></div>
          <div class="journey-result"><b>{{ version.rule_count }} 条规则</b><small v-if="version.metrics_json?.total">{{ version.metrics_json.passed }}/{{ version.metrics_json.total }} 验证通过</small><small v-else>未记录回归结果</small></div>
        </article>
      </div>
    </section>

    <section class="workspace ccc-card">
      <div class="workspace-heading"><div><span>管理员工作区</span><h2>审核迭代管理</h2></div><p>处理纠错、维护规则、发布版本和查看技术依据。</p></div>
      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <el-tab-pane name="corrections">
          <template #label><span class="tab-label"><el-icon><EditPen /></el-icon>纠错确认</span></template>
          <div class="toolbar">
            <el-select v-model="correctionFilter" clearable placeholder="全部状态" style="width: 150px" @change="loadCorrections">
              <el-option label="待确认" value="pending" />
              <el-option label="已通过" value="approved" />
              <el-option label="已驳回" value="rejected" />
            </el-select>
            <el-button :icon="Refresh" @click="loadCorrections">刷新</el-button>
          </div>
          <el-table v-loading="correctionsLoading" :data="corrections" stripe>
            <el-table-column prop="report_label" label="申请编号" min-width="190" show-overflow-tooltip />
            <el-table-column prop="company_name" label="企业" min-width="160" show-overflow-tooltip />
            <el-table-column prop="correction_type_text" label="纠错类型" width="130" />
            <el-table-column prop="description" label="错误说明" min-width="230" show-overflow-tooltip />
            <el-table-column label="提交人/时间" width="165">
              <template #default="{ row }"><div>{{ row.submitter_name }}</div><small>{{ formatTime(row.created_at) }}</small></template>
            </el-table-column>
            <el-table-column label="状态" width="90" align="center">
              <template #default="{ row }"><el-tag :type="correctionStatusType(row.status)">{{ correctionStatusText(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="110" fixed="right">
              <template #default="{ row }"><el-button link type="primary" @click="openCorrectionReview(row)">{{ row.status === 'pending' ? '确认处理' : '查看' }}</el-button></template>
            </el-table-column>
            <template #empty><el-empty description="暂无纠错记录" :image-size="80" /></template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane name="rules">
          <template #label><span class="tab-label"><el-icon><DocumentChecked /></el-icon>审核规则</span></template>
          <div class="toolbar toolbar-between">
            <span class="toolbar-note">只有被纳入活动版本的规则才会影响审核。</span>
            <el-button type="primary" :icon="Plus" @click="openRuleDialog()">新增规则</el-button>
          </div>
          <el-table v-loading="rulesLoading" :data="rules" stripe>
            <el-table-column prop="title" label="规则名称" min-width="220" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="120" />
            <el-table-column prop="product_unit" label="适用产品单元" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.product_unit || '全部' }}</template>
            </el-table-column>
            <el-table-column prop="rule_text" label="规则内容" min-width="260" show-overflow-tooltip />
            <el-table-column label="状态" width="90" align="center">
              <template #default="{ row }"><el-tag :type="ruleStatusType(row.status)">{{ ruleStatusText(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="更新时间" width="155"><template #default="{ row }">{{ formatTime(row.updated_at) }}</template></el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" @click="openRuleDialog(row)">编辑</el-button>
                <el-button v-if="row.status !== 'retired'" link type="danger" @click="retireRule(row)">停用</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无审核规则，可从已确认纠错生成" :image-size="80" /></template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane name="versions">
          <template #label><span class="tab-label"><el-icon><Files /></el-icon>版本发布</span></template>
          <div class="active-version" v-if="summary.active_version">
            <div><span>当前活动版本</span><strong>V{{ summary.active_version.version_no }} {{ summary.active_version.name }}</strong></div>
            <p>{{ summary.active_version.description || '当前正式审核使用此版本中的规则。' }}</p>
          </div>
          <div class="toolbar toolbar-between">
            <span class="toolbar-note">创建版本时会保存规则快照，之后可以回退到任一历史版本。</span>
            <el-button type="primary" :icon="Plus" :disabled="!availableRules.length" @click="openVersionDialog">创建版本</el-button>
          </div>
          <el-table v-loading="versionsLoading" :data="versions" stripe>
            <el-table-column label="版本" width="90"><template #default="{ row }">V{{ row.version_no }}</template></el-table-column>
            <el-table-column prop="name" label="名称" min-width="180" />
            <el-table-column prop="description" label="说明" min-width="220" show-overflow-tooltip />
            <el-table-column prop="rule_count" label="规则数" width="90" align="center" />
            <el-table-column label="回归结果" width="120" align="center">
              <template #default="{ row }"><span v-if="row.metrics_json?.total">{{ row.metrics_json.passed }}/{{ row.metrics_json.total }} 通过</span><span v-else>-</span></template>
            </el-table-column>
            <el-table-column label="状态" width="90" align="center">
              <template #default="{ row }"><el-tag :type="versionStatusType(row.status)">{{ versionStatusText(row.status) }}</el-tag></template>
            </el-table-column>
            <el-table-column label="发布时间" width="155"><template #default="{ row }">{{ formatTime(row.published_at) }}</template></el-table-column>
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.status === 'draft'" link type="primary" @click="publish(row)">发布</el-button>
                <el-button v-else-if="row.status !== 'active'" link type="warning" @click="rollback(row)">回退到此版本</el-button>
                <span v-else class="current-text">当前使用</span>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无版本，请先准备审核规则" :image-size="80" /></template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane name="regression">
          <template #label><span class="tab-label"><el-icon><DataAnalysis /></el-icon>回归校验</span></template>
          <div class="toolbar toolbar-between">
            <span class="toolbar-note">用人工确认的期望结论检查当前报告结果，发布新版本后建议重新审核样本再执行校验。</span>
            <div><el-button :icon="Plus" @click="openCaseDialog">加入样本</el-button><el-button type="primary" :icon="CaretRight" :disabled="!enabledCases.length" :loading="runningRegression" @click="runCheck">执行校验</el-button></div>
          </div>
          <el-table v-loading="casesLoading" :data="regressionCases" stripe>
            <el-table-column prop="name" label="样本名称" min-width="180" />
            <el-table-column prop="report_label" label="申请编号" min-width="190" />
            <el-table-column prop="expected_conclusion" label="期望结论" width="130" />
            <el-table-column prop="expected_issue_count" label="期望问题数" width="110" align="center" />
            <el-table-column prop="notes" label="说明" min-width="200" show-overflow-tooltip />
            <el-table-column label="启用" width="80" align="center"><template #default="{ row }"><el-switch v-model="row.enabled" @change="toggleCase(row)" /></template></el-table-column>
            <template #empty><el-empty description="尚未建立回归测试集" :image-size="80" /></template>
          </el-table>

          <div class="subsection-title">校验记录</div>
          <el-table :data="regressionRuns" stripe>
            <el-table-column type="expand">
              <template #default="{ row }">
                <el-table :data="row.results_json" size="small" class="result-table">
                  <el-table-column prop="report_label" label="申请编号" min-width="180" />
                  <el-table-column label="期望/实际结论" min-width="220"><template #default="scope">{{ scope.row.expected_conclusion }} / {{ scope.row.actual_conclusion }}</template></el-table-column>
                  <el-table-column label="期望/实际问题数" width="160" align="center"><template #default="scope">{{ scope.row.expected_issue_count }} / {{ scope.row.actual_issue_count }}</template></el-table-column>
                  <el-table-column label="结果" width="90" align="center"><template #default="scope"><el-tag :type="scope.row.passed ? 'success' : 'danger'">{{ scope.row.passed ? '通过' : '未通过' }}</el-tag></template></el-table-column>
                </el-table>
              </template>
            </el-table-column>
            <el-table-column prop="id" label="批次" width="80"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
            <el-table-column label="通过情况" min-width="160"><template #default="{ row }"><b>{{ row.passed }}</b> / {{ row.total }} 通过</template></el-table-column>
            <el-table-column prop="failed" label="未通过" width="100" />
            <el-table-column label="执行时间" width="165"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
            <template #empty><el-empty description="暂无校验记录" :image-size="70" /></template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane name="materials">
          <template #label><span class="tab-label"><el-icon><Collection /></el-icon>技术资料</span></template>
          <div class="materials-intro">
            <div><strong>审核依据与规则资产</strong><p>供管理员追溯标准依据、参数规则和版本变更，不影响领导看板的结论展示。</p></div>
            <div class="material-versions"><span>依据库 {{ summary.component_versions?.knowledge_version || '-' }}</span><span>规则库 {{ summary.component_versions?.structured_rulebase_version || '-' }}</span></div>
          </div>
          <el-tabs v-model="materialActiveTab" class="material-tabs" @tab-change="onMaterialTabChange">
            <el-tab-pane name="knowledge" label="标准依据库">
          <div class="knowledge-browser">
            <div class="knowledge-tree">
              <div class="toolbar">
                <span class="toolbar-note">Markdown 知识库 {{ summary.component_versions?.knowledge_version || '' }}</span>
                <el-button :icon="Refresh" size="small" @click="loadKnowledge">刷新</el-button>
              </div>
              <el-tree
                :data="knowledgeTree"
                :props="{ label: 'name', children: 'children' }"
                :default-expand-all="false"
                @node-click="onKnowledgeNodeClick"
                highlight-current
              >
                <template #default="{ node, data }">
                  <span class="tree-node">
                    <el-icon v-if="data.is_file"><Document /></el-icon>
                    <el-icon v-else><Folder /></el-icon>
                    <span>{{ node.label }}</span>
                  </span>
                </template>
              </el-tree>
            </div>
            <div class="knowledge-preview">
              <div v-if="knowledgeContent" class="markdown-body" v-html="renderedMarkdown"></div>
              <el-empty v-else description="点击左侧文件查看内容" :image-size="80" />
            </div>
          </div>
            </el-tab-pane>

            <el-tab-pane name="rulebase" label="结构化规则库">
          <div v-loading="rulebaseLoading">
            <el-tabs v-model="rulebaseActiveSubTab" type="border-card">
              <el-tab-pane name="parameters" label="参数规则">
                <el-table :data="rulebaseData.parameter_rules" stripe size="small">
                  <el-table-column prop="id" label="ID" width="60" />
                  <el-table-column prop="parameter_name" label="参数名" min-width="140" />
                  <el-table-column prop="operator" label="运算符" width="70" />
                  <el-table-column prop="parameter_value" label="标准值" width="100" />
                  <el-table-column prop="unit" label="单位" width="70" />
                  <el-table-column prop="applicable_conditions" label="适用条件" min-width="200" show-overflow-tooltip />
                  <el-table-column prop="evidence_text" label="标准依据" min-width="220" show-overflow-tooltip />
                  <el-table-column prop="verification_status" label="状态" width="90" />
                </el-table>
                <el-empty v-if="!rulebaseData.parameter_rules.length" description="暂无结构化参数规则（当前使用SQLite，规则库表在PostgreSQL中）" :image-size="80" />
              </el-tab-pane>
              <el-tab-pane name="versions" label="版本记录">
                <el-table :data="rulebaseData.rule_versions" stripe size="small">
                  <el-table-column prop="id" label="ID" width="60" />
                  <el-table-column prop="version_no" label="版本号" width="100" />
                  <el-table-column prop="name" label="名称" min-width="180" />
                  <el-table-column prop="description" label="说明" min-width="220" show-overflow-tooltip />
                  <el-table-column prop="status" label="状态" width="90" />
                  <el-table-column prop="created_at" label="创建时间" width="165" />
                </el-table>
                <el-empty v-if="!rulebaseData.rule_versions.length" description="暂无版本记录" :image-size="80" />
              </el-tab-pane>
              <el-tab-pane name="changelog" label="变更日志">
                <el-table :data="rulebaseData.rule_change_log" stripe size="small">
                  <el-table-column prop="id" label="ID" width="60" />
                  <el-table-column prop="change_version" label="变更版本" width="140" />
                  <el-table-column prop="change_type" label="类型" width="90" />
                  <el-table-column prop="target_table" label="目标表" width="120" />
                  <el-table-column prop="field_changed" label="变更字段" min-width="160" />
                  <el-table-column prop="old_value" label="旧值" min-width="120" show-overflow-tooltip />
                  <el-table-column prop="new_value" label="新值" min-width="120" show-overflow-tooltip />
                  <el-table-column prop="change_reason" label="原因" min-width="200" show-overflow-tooltip />
                  <el-table-column prop="changed_at" label="时间" width="165" />
                </el-table>
                <el-empty v-if="!rulebaseData.rule_change_log.length" description="暂无变更日志" :image-size="80" />
              </el-tab-pane>
            </el-tabs>
          </div>
            </el-tab-pane>
          </el-tabs>
        </el-tab-pane>
      </el-tabs>
    </section>

    <el-dialog v-model="correctionDialog.visible" :title="correctionDialog.readonly ? '查看纠错' : '确认纠错'" width="720px" destroy-on-close>
      <div v-if="selectedCorrection" class="correction-detail">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="申请编号">{{ selectedCorrection.report_label }}</el-descriptions-item>
          <el-descriptions-item label="纠错类型">{{ selectedCorrection.correction_type_text }}</el-descriptions-item>
          <el-descriptions-item label="错误说明" :span="2">{{ selectedCorrection.description }}</el-descriptions-item>
          <el-descriptions-item label="标准依据" :span="2">{{ selectedCorrection.standard_ref || '-' }}</el-descriptions-item>
        </el-descriptions>
        <div class="json-compare"><div><label>AI 原结果</label><pre>{{ prettyJson(selectedCorrection.original_json) }}</pre></div><div><label>人工正确结果</label><pre>{{ prettyJson(selectedCorrection.corrected_json) }}</pre></div></div>
        <el-form v-if="!correctionDialog.readonly" label-position="top">
          <el-form-item><el-checkbox v-model="correctionDialog.create_rule">通过后生成草稿规则</el-checkbox></el-form-item>
          <template v-if="correctionDialog.create_rule">
            <el-form-item label="规则名称"><el-input v-model="correctionDialog.rule_title" /></el-form-item>
            <el-form-item label="适用条件"><el-input v-model="correctionDialog.applicable_conditions" type="textarea" :rows="2" placeholder="例如：仅适用于 RVV、外径不大于 12.5mm 的样品" /></el-form-item>
            <el-form-item label="正确审核规则"><el-input v-model="correctionDialog.rule_text" type="textarea" :rows="3" /></el-form-item>
          </template>
          <el-form-item label="管理员意见"><el-input v-model="correctionDialog.review_note" type="textarea" :rows="2" /></el-form-item>
        </el-form>
        <el-alert v-else :type="selectedCorrection.status === 'approved' ? 'success' : 'warning'" :closable="false" :title="`处理结果：${correctionStatusText(selectedCorrection.status)}${selectedCorrection.review_note ? `，${selectedCorrection.review_note}` : ''}`" />
      </div>
      <template #footer>
        <el-button @click="correctionDialog.visible = false">关闭</el-button>
        <template v-if="!correctionDialog.readonly"><el-button type="danger" plain :loading="correctionDialog.saving" @click="submitCorrectionReview('rejected')">驳回</el-button><el-button type="primary" :loading="correctionDialog.saving" @click="submitCorrectionReview('approved')">确认通过</el-button></template>
      </template>
    </el-dialog>

    <el-dialog v-model="ruleDialog.visible" :title="ruleDialog.id ? '编辑审核规则' : '新增审核规则'" width="680px" destroy-on-close>
      <el-form label-position="top">
        <div class="form-grid"><el-form-item label="规则名称"><el-input v-model="ruleDialog.title" /></el-form-item><el-form-item label="分类"><el-input v-model="ruleDialog.category" /></el-form-item></div>
        <el-form-item label="适用产品单元"><el-input v-model="ruleDialog.product_unit" placeholder="留空表示全部产品单元" /></el-form-item>
        <el-form-item label="适用条件"><el-input v-model="ruleDialog.applicable_conditions" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="正确审核规则"><el-input v-model="ruleDialog.rule_text" type="textarea" :rows="4" /></el-form-item>
        <el-form-item label="标准依据"><el-input v-model="ruleDialog.standard_ref" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="ruleDialog.visible = false">取消</el-button><el-button type="primary" :loading="ruleDialog.saving" @click="saveRule">保存草稿</el-button></template>
    </el-dialog>

    <el-dialog v-model="versionDialog.visible" title="创建审核版本" width="680px" destroy-on-close>
      <el-form label-position="top"><el-form-item label="版本名称"><el-input v-model="versionDialog.name" placeholder="例如：低温试验规则修订" /></el-form-item><el-form-item label="版本说明"><el-input v-model="versionDialog.description" type="textarea" :rows="2" /></el-form-item><el-form-item label="纳入规则"><el-checkbox-group v-model="versionDialog.rule_ids" class="rule-picker"><el-checkbox v-for="rule in availableRules" :key="rule.id" :value="rule.id"><b>{{ rule.title }}</b><span>{{ rule.product_unit || '全部产品单元' }}</span></el-checkbox></el-checkbox-group></el-form-item></el-form>
      <template #footer><el-button @click="versionDialog.visible = false">取消</el-button><el-button type="primary" :loading="versionDialog.saving" @click="saveVersion">创建版本</el-button></template>
    </el-dialog>

    <el-dialog v-model="caseDialog.visible" title="加入回归测试样本" width="600px" destroy-on-close>
      <el-form label-position="top"><el-form-item label="选择报告"><el-select v-model="caseDialog.report_id" filterable style="width:100%"><el-option v-for="report in reportOptions" :key="report.id" :label="`${report.application_no || report.report_no} - ${report.company?.name || ''}`" :value="report.id" /></el-select></el-form-item><el-form-item label="样本名称"><el-input v-model="caseDialog.name" /></el-form-item><div class="form-grid"><el-form-item label="人工确认的结论"><el-select v-model="caseDialog.expected_conclusion"><el-option label="合格" value="合格" /><el-option v-for="n in 20" :key="n" :label="`需修改${n}处`" :value="`需修改${n}处`" /></el-select></el-form-item><el-form-item label="人工确认的问题数"><el-input-number v-model="caseDialog.expected_issue_count" :min="0" :max="999" /></el-form-item></div><el-form-item label="说明"><el-input v-model="caseDialog.notes" type="textarea" :rows="2" /></el-form-item></el-form>
      <template #footer><el-button @click="caseDialog.visible = false">取消</el-button><el-button type="primary" :loading="caseDialog.saving" @click="saveCase">加入测试集</el-button></template>
    </el-dialog>

    <el-dialog v-model="atlasDialogVisible" fullscreen class="atlas-dialog" destroy-on-close>
      <template #header><div class="atlas-dialog-title"><div><span>全景展示</span><strong>CCC电线电缆审核规则知识图谱</strong></div><small>{{ rulebaseTreeData.summary?.version }}，共 {{ rulebaseTreeData.summary?.total_rules || 0 }} 条启用规则</small></div></template>
      <div class="atlas-fullscreen atlas-fullscreen-layout">
        <aside class="atlas-overview atlas-overview-full" v-if="rulebaseTreeData.summary">
          <div class="overview-title"><strong>规则库概览</strong><span>当前启用数据</span></div>
          <div class="overview-stat"><i class="stat-blue"><el-icon><Collection /></el-icon></i><span>标准体系<strong>{{ rulebaseTreeData.summary.series || 0 }}</strong></span></div>
          <div class="overview-stat"><i class="stat-cyan"><el-icon><Document /></el-icon></i><span>产品/方法标准<strong>{{ rulebaseTreeData.summary.standards || 0 }}</strong></span></div>
          <div class="overview-stat"><i class="stat-green"><el-icon><Files /></el-icon></i><span>产品型号<strong>{{ rulebaseTreeData.summary.models || 0 }}</strong></span></div>
          <div class="overview-stat"><i class="stat-orange"><el-icon><DataAnalysis /></el-icon></i><span>试验项目<strong>{{ rulebaseTreeData.summary.test_items || 0 }}</strong></span></div>
          <div class="overview-stat overview-total"><i><el-icon><CircleCheck /></el-icon></i><span>启用结构化规则<strong>{{ rulebaseTreeData.summary.total_rules || 0 }}</strong></span></div>
        </aside>
        <div class="atlas-canvas atlas-canvas-full">
          <VChart v-if="filteredRuleTree" :option="ruleTreeFullscreenOption" height="calc(100dvh - 82px)" @node-click="selectRuleNode" />
        </div>
        <aside class="atlas-detail atlas-detail-full">
          <div class="detail-heading"><span>节点信息</span><b>{{ ruleNodeTypeText(selectedRuleNode?.type) }}</b></div>
          <h3>{{ selectedRuleNode?.name || '点击图谱节点' }}</h3>
          <p v-if="selectedRuleNode?.count">下辖 <b>{{ selectedRuleNode.count }}</b> 条最终规则</p>
          <dl v-if="selectedRuleMeta.length">
            <template v-for="entry in selectedRuleMeta" :key="entry[0]">
              <dt>{{ ruleMetaLabel(entry[0]) }}</dt><dd>{{ entry[1] || '-' }}</dd>
            </template>
          </dl>
          <div v-else class="detail-hint">点击任意节点，查看真实规则关系与标准依据。</div>
        </aside>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CaretRight, CircleCheck, Collection, DataAnalysis, Document, DocumentChecked, EditPen, Files, Folder, FullScreen, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { formatSystemTime } from '@/utils/datetime'
import { useAppStore } from '@/stores/app'
import { getReports } from '@/api/reports'
import type { ReportItem } from '@/types'
import VChart from '@/components/VChart.vue'
import {
  createRegressionCase, createRule, createVersion, getCorrections, getIterationSummary,
  getKnowledgeFile, getKnowledgeTree, getRegressionCases, getRegressionRuns, getRules, getRulebase, getRulebaseTree,
  getVersions, publishVersion, reviewCorrection, rollbackVersion, runRegression, toggleRegressionCase, updateRule,
  type CorrectionRecord, type KnowledgeNode, type RegressionCaseRecord, type RegressionRunRecord,
  type ReviewRuleRecord, type ReviewVersionRecord, type RulebaseData, type RulebaseTreeData, type RulebaseTreeNode
} from '@/api/iteration'

const app = useAppStore()
const activeTab = ref('corrections')
const loading = ref(false)
const summary = reactive<any>({})
const corrections = ref<CorrectionRecord[]>([])
const allCorrections = ref<CorrectionRecord[]>([])
const rules = ref<ReviewRuleRecord[]>([])
const versions = ref<ReviewVersionRecord[]>([])
const regressionCases = ref<RegressionCaseRecord[]>([])
const regressionRuns = ref<RegressionRunRecord[]>([])
const reportOptions = ref<ReportItem[]>([])
const correctionFilter = ref('pending')
const correctionsLoading = ref(false)
const rulesLoading = ref(false)
const versionsLoading = ref(false)
const casesLoading = ref(false)
const runningRegression = ref(false)
const selectedCorrection = ref<CorrectionRecord | null>(null)

const availableRules = computed(() => rules.value.filter((rule) => rule.status !== 'retired'))
const enabledCases = computed(() => regressionCases.value.filter((item) => item.enabled))

const correctionDialog = reactive({ visible: false, readonly: false, saving: false, create_rule: true, rule_title: '', applicable_conditions: '', rule_text: '', review_note: '' })
const ruleDialog = reactive({ visible: false, saving: false, id: 0, title: '', category: '人工纠错', product_unit: '', applicable_conditions: '', rule_text: '', standard_ref: '' })
const versionDialog = reactive({ visible: false, saving: false, name: '', description: '', rule_ids: [] as number[] })
const caseDialog = reactive({ visible: false, saving: false, report_id: 0, name: '', expected_conclusion: '合格', expected_issue_count: 0, notes: '' })

// 知识库
const knowledgeTree = ref<KnowledgeNode[]>([])
const knowledgeContent = ref('')
const renderedMarkdown = computed(() => knowledgeContent.value ? marked.parse(knowledgeContent.value, { async: false }) as string : '')

// 结构化规则库
const rulebaseLoading = ref(false)
const materialActiveTab = ref('knowledge')
const rulebaseActiveSubTab = ref('parameters')
const rulebaseData = reactive<RulebaseData>({ parameter_rules: [], rule_versions: [], rule_change_log: [] })
const rulebaseTreeLoading = ref(false)
const rulebaseTreeData = reactive<any>({ summary: {}, tree: null })
const selectedRuleNode = ref<RulebaseTreeNode | null>(null)
const ruleGraphKey = ref(0)
const atlasDialogVisible = ref(false)
const atlasSearch = ref('')
const atlasFilter = ref('all')
const atlasFilterOptions = [
  { label: '全部', value: 'all' },
  { label: '材料体系', value: 'material_family' },
  { label: '标准系列', value: 'standard_series' },
  { label: '产品型号', value: 'product_model' },
  { label: '试验项目', value: 'test_item' },
  { label: '最终规则', value: 'final_rule' },
]

const latestRegression = computed(() => regressionRuns.value[0] || summary.last_run || null)
const activeRuleCount = computed(() => summary.active_version?.rule_count || rules.value.filter((rule) => rule.status === 'active').length)
const approvedCorrectionCount = computed(() => allCorrections.value.filter((item) => item.status === 'approved').length)
const openRiskCount = computed(() => Number(summary.pending_corrections || 0) + Number(latestRegression.value?.failed || 0))
const regressionSummary = computed(() => latestRegression.value?.total ? `${latestRegression.value.passed}/${latestRegression.value.total}` : '待验证')
const regressionStatusText = computed(() => {
  if (!latestRegression.value?.total) return '尚未执行回归校验'
  return latestRegression.value.failed ? `${latestRegression.value.failed} 项未通过，暂不建议发布` : `${latestRegression.value.passed} 项全部通过，版本验证正常`
})
const currentVersionTitle = computed(() => summary.active_version ? `V${summary.active_version.version_no} ${summary.active_version.name}` : '基础审核版本')
const currentVersionDescription = computed(() => summary.active_version?.description || '当前审核规则已通过管理员确认并正式生效。')
const recentVersions = computed(() => versions.value.slice(0, 5))
const selectedRuleMeta = computed(() => Object.entries(selectedRuleNode.value?.meta || {}).filter(([, value]) => value !== '' && value !== null && value !== undefined))

const nodeColors: Record<string, string> = {
  root: '#2563eb', rule_layer: '#3b82f6', material_family: '#14b8a6',
  standard_series: '#0ea5e9', product_standard: '#38bdf8', product_model: '#22a879',
  test_category: '#f59e0b', test_item: '#f97316', test_method: '#f4b740',
  applicability_rule: '#ef6b73', parameter_rule: '#8b5cf6',
}

function cloneRuleTree(source: RulebaseTreeNode): RulebaseTreeNode {
  return { ...source, meta: { ...(source.meta || {}) }, children: (source.children || []).map(cloneRuleTree) }
}

function focusRuleTree(source: RulebaseTreeNode, targetTypes: string[], inheritedMatch = false): RulebaseTreeNode | null {
  const directMatch = inheritedMatch || targetTypes.includes(source.type)
  const children = (source.children || [])
    .map((child) => focusRuleTree(child, targetTypes, directMatch))
    .filter((child): child is RulebaseTreeNode => Boolean(child))
  if (!directMatch && !children.length) return null
  return { ...source, meta: { ...(source.meta || {}) }, children: directMatch ? (source.children || []).map(cloneRuleTree) : children }
}

function searchRuleTree(source: RulebaseTreeNode, keyword: string): RulebaseTreeNode | null {
  const haystack = `${source.name} ${ruleNodeTypeText(source.type)} ${JSON.stringify(source.meta || {})}`.toLowerCase()
  const directMatch = source.type !== 'root' && haystack.includes(keyword)
  const children = (source.children || [])
    .map((child) => searchRuleTree(child, keyword))
    .filter((child): child is RulebaseTreeNode => Boolean(child))
  if (!directMatch && !children.length) return null
  return { ...source, meta: { ...(source.meta || {}) }, children: directMatch ? (source.children || []).map(cloneRuleTree) : children }
}

const filteredRuleTree = computed<RulebaseTreeNode | null>(() => {
  if (!rulebaseTreeData.tree) return null
  const filterTypes = atlasFilter.value === 'all'
    ? []
    : atlasFilter.value === 'final_rule'
      ? ['applicability_rule', 'parameter_rule']
      : [atlasFilter.value]
  let tree = filterTypes.length ? focusRuleTree(rulebaseTreeData.tree, filterTypes) : cloneRuleTree(rulebaseTreeData.tree)
  const keyword = atlasSearch.value.trim().toLowerCase()
  if (tree && keyword) tree = searchRuleTree(tree, keyword)
  return tree
})

function decorateRuleTree(source: RulebaseTreeNode, depth = 0): Record<string, any> {
  const color = nodeColors[source.type] || '#64748b'
  const sizes: Record<string, number> = { root: 30, rule_layer: 22, material_family: 17, standard_series: 15, product_standard: 13, product_model: 12, test_category: 12, test_item: 11, test_method: 10, applicability_rule: 8, parameter_rule: 8 }
  return {
    ...source,
    symbolSize: sizes[source.type] || 8,
    itemStyle: { color, borderColor: '#f8fbff', borderWidth: depth < 2 ? 3 : 1.5, shadowBlur: depth < 3 ? 10 : 4, shadowColor: `${color}45` },
    lineStyle: { color: `${color}75`, width: depth < 3 ? 1.5 : 1 },
    label: { color: depth < 3 ? '#183153' : '#334155', fontWeight: depth < 3 ? 650 : 500 },
    children: (source.children || []).map((item) => decorateRuleTree(item, depth + 1)),
  }
}

function escapeTooltip(value: unknown) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character] || character))
}

function buildRuleTreeOption(fullscreen = false) {
  if (!filteredRuleTree.value) return {}
  return {
    backgroundColor: 'transparent',
    animationDuration: 420,
    animationDurationUpdate: 320,
    tooltip: {
      trigger: 'item', triggerOn: 'mousemove', confine: true,
      backgroundColor: 'rgba(255, 255, 255, .98)', borderColor: '#dce7f3',
      textStyle: { color: '#1e293b', fontSize: 12 },
      extraCssText: 'max-width:360px;box-shadow:0 14px 34px rgba(34,76,130,.14);border-radius:10px;padding:12px 14px;',
      formatter: (params: any) => {
        const data = params.data || {}
        const type = escapeTooltip(ruleNodeTypeText(data.type))
        const status = data.meta?.status ? `<br/><span style="color:#718096">状态</span> ${escapeTooltip(data.meta.status)}` : ''
        const value = data.meta?.value ? `<br/><span style="color:#718096">标准值</span> ${escapeTooltip(data.meta.value)}` : ''
        const count = data.count ? `<br/><span style="color:#718096">下辖规则</span> ${escapeTooltip(data.count)}` : ''
        return `<b>${escapeTooltip(data.name || '')}</b><br/><span style="color:#2563eb">${type}</span>${count}${status}${value}`
      },
    },
    series: [{
      type: 'tree',
      data: [decorateRuleTree(filteredRuleTree.value)],
      top: fullscreen ? 42 : 42, left: fullscreen ? 78 : 44, bottom: fullscreen ? 42 : 72, right: fullscreen ? 150 : 135,
      orient: 'LR', symbol: 'circle', roam: true, expandAndCollapse: true,
      initialTreeDepth: fullscreen ? 3 : 2,
      edgeShape: 'polyline', edgeForkPosition: '58%',
      lineStyle: { color: 'rgba(65, 116, 177, .34)', width: 1.1, curveness: 0 },
      label: {
        position: 'right', distance: 7, verticalAlign: 'middle', align: 'left',
        color: '#334155', fontSize: fullscreen ? 12 : 11,
        formatter: (params: any) => {
          const name = String(params.data?.name || '')
          const limit = fullscreen ? 34 : 25
          return name.length > limit ? `${name.slice(0, limit)}…` : name
        },
      },
      leaves: { label: { position: 'right', color: '#475569' } },
      emphasis: { focus: 'descendant', lineStyle: { width: 2, color: '#2563eb' }, itemStyle: { borderWidth: 3, borderColor: '#ffffff' } },
    }],
  }
}

const ruleTreeOption = computed(() => buildRuleTreeOption(false))
const ruleTreeFullscreenOption = computed(() => buildRuleTreeOption(true))

function formatTime(value?: string | null) { return formatSystemTime(value, app.systemConfig.timezone) }
function prettyJson(value: Record<string, any>) { return Object.keys(value || {}).length ? JSON.stringify(value, null, 2) : '无' }
function correctionStatusText(status: string) { return ({ pending: '待确认', approved: '已通过', rejected: '已驳回' } as Record<string, string>)[status] || status }
function correctionStatusType(status: string): 'warning' | 'success' | 'danger' { return status === 'approved' ? 'success' : status === 'rejected' ? 'danger' : 'warning' }
function ruleStatusText(status: string) { return ({ draft: '草稿', active: '已发布', retired: '已停用' } as Record<string, string>)[status] || status }
function ruleStatusType(status: string): 'info' | 'success' | 'danger' { return status === 'active' ? 'success' : status === 'retired' ? 'danger' : 'info' }
function versionStatusText(status: string) { return ({ draft: '草稿', active: '活动', archived: '历史' } as Record<string, string>)[status] || status }
function versionStatusType(status: string): 'info' | 'success' | 'warning' { return status === 'active' ? 'success' : status === 'archived' ? 'warning' : 'info' }
function ruleNodeTypeText(type?: string) { return ({ root: '规则库总览', rule_layer: '规则层', material_family: '产品材料体系', standard_series: '标准系列', product_standard: '产品标准', product_model: '产品型号', test_category: '试验类别', test_item: '试验项目', test_method: '试验方法', applicability_rule: '型号适用规则', parameter_rule: '试验参数规则' } as Record<string, string>)[type || ''] || '规则节点' }
function ruleMetaLabel(key: string) { return ({ description: '说明', code: '体系代码', model_name: '型号名称', product_unit: '产品单元', rule_id: '规则ID', status: '适用性', condition: '适用条件', table_item: '表号/项次', evidence: '标准依据', verification: '验证状态', standard: '依据标准', model: '适用型号', clause: '条款', parameter: '参数名称', value: '标准值' } as Record<string, string>)[key] || key }
function selectRuleNode(data: Record<string, any>) { selectedRuleNode.value = data as RulebaseTreeNode }
function resetRuleGraph() { atlasSearch.value = ''; atlasFilter.value = 'all'; ruleGraphKey.value += 1; selectedRuleNode.value = rulebaseTreeData.tree }

async function loadSummary() { Object.assign(summary, await getIterationSummary()) }
async function loadCorrections() { correctionsLoading.value = true; try { corrections.value = await getCorrections(correctionFilter.value) } finally { correctionsLoading.value = false } }
async function loadAllCorrections() { allCorrections.value = await getCorrections('') }
async function loadRules() { rulesLoading.value = true; try { rules.value = await getRules() } finally { rulesLoading.value = false } }
async function loadVersions() { versionsLoading.value = true; try { versions.value = await getVersions() } finally { versionsLoading.value = false } }
async function loadRegression() { casesLoading.value = true; try { [regressionCases.value, regressionRuns.value] = await Promise.all([getRegressionCases(), getRegressionRuns()]) } finally { casesLoading.value = false } }
async function loadRulebaseTree() { rulebaseTreeLoading.value = true; try { const data: RulebaseTreeData = await getRulebaseTree(); Object.assign(rulebaseTreeData, data); selectedRuleNode.value = data.tree } finally { rulebaseTreeLoading.value = false } }
async function loadAll() { loading.value = true; try { await Promise.all([loadSummary(), loadCorrections(), loadAllCorrections(), loadRules(), loadVersions(), loadRegression(), loadRulebaseTree()]) } finally { loading.value = false } }

function onTabChange(name: string | number) {
  if (name === 'corrections') loadCorrections()
  if (name === 'rules') loadRules()
  if (name === 'versions') Promise.all([loadRules(), loadVersions()])
  if (name === 'regression') loadRegression()
  if (name === 'materials') onMaterialTabChange(materialActiveTab.value)
}

function onMaterialTabChange(name: string | number) {
  if (name === 'knowledge') loadKnowledge()
  if (name === 'rulebase') loadRulebase()
}

// 知识库
async function loadKnowledge() {
  knowledgeTree.value = await getKnowledgeTree()
}
async function onKnowledgeNodeClick(data: KnowledgeNode) {
  if (!data.is_file) return
  const file = await getKnowledgeFile(data.path)
  knowledgeContent.value = file.content
}

// 结构化规则库
async function loadRulebase() {
  rulebaseLoading.value = true
  try {
    const data = await getRulebase()
    Object.assign(rulebaseData, data)
  } finally {
    rulebaseLoading.value = false
  }
}

function openCorrectionReview(row: CorrectionRecord) { selectedCorrection.value = row; Object.assign(correctionDialog, { visible: true, readonly: row.status !== 'pending', saving: false, create_rule: true, rule_title: `${row.correction_type_text}：${row.description.slice(0, 60)}`, applicable_conditions: row.product_unit ? `适用于${row.product_unit}，并满足本纠错记录描述的条件` : '', rule_text: row.description, review_note: '' }) }
async function submitCorrectionReview(status: 'approved' | 'rejected') { if (!selectedCorrection.value) return; correctionDialog.saving = true; try { await reviewCorrection(selectedCorrection.value.id, { status, review_note: correctionDialog.review_note, create_rule: status === 'approved' && correctionDialog.create_rule, rule_title: correctionDialog.rule_title, applicable_conditions: correctionDialog.applicable_conditions, rule_text: correctionDialog.rule_text }); correctionDialog.visible = false; await Promise.all([loadSummary(), loadCorrections(), loadAllCorrections(), loadRules()]); ElMessage.success(status === 'approved' ? '纠错已确认，草稿规则已生成' : '纠错已驳回') } finally { correctionDialog.saving = false } }

function openRuleDialog(row?: ReviewRuleRecord) { Object.assign(ruleDialog, { visible: true, saving: false, id: row?.id || 0, title: row?.title || '', category: row?.category || '人工纠错', product_unit: row?.product_unit || '', applicable_conditions: row?.applicable_conditions || '', rule_text: row?.rule_text || '', standard_ref: row?.standard_ref || '' }) }
async function saveRule() { if (!ruleDialog.title.trim() || !ruleDialog.rule_text.trim()) return void ElMessage.warning('请填写规则名称和规则内容'); ruleDialog.saving = true; try { const data = { title: ruleDialog.title, category: ruleDialog.category, product_unit: ruleDialog.product_unit, applicable_conditions: ruleDialog.applicable_conditions, rule_text: ruleDialog.rule_text, standard_ref: ruleDialog.standard_ref }; if (ruleDialog.id) await updateRule(ruleDialog.id, data); else await createRule(data); ruleDialog.visible = false; await Promise.all([loadRules(), loadSummary()]); ElMessage.success('规则草稿已保存') } finally { ruleDialog.saving = false } }
async function retireRule(row: ReviewRuleRecord) { await ElMessageBox.confirm(`确认停用规则"${row.title}"吗？已发布版本的历史快照不会被修改。`, '停用规则', { type: 'warning' }); await updateRule(row.id, { status: 'retired' }); await loadRules(); ElMessage.success('规则已停用') }

function openVersionDialog() { Object.assign(versionDialog, { visible: true, saving: false, name: '', description: '', rule_ids: availableRules.value.map((item) => item.id) }) }
async function saveVersion() { if (!versionDialog.name.trim()) return void ElMessage.warning('请填写版本名称'); if (!versionDialog.rule_ids.length) return void ElMessage.warning('请至少选择一条规则'); versionDialog.saving = true; try { await createVersion({ name: versionDialog.name, description: versionDialog.description, rule_ids: versionDialog.rule_ids }); versionDialog.visible = false; await loadVersions(); ElMessage.success('版本草稿已创建，请确认后发布') } finally { versionDialog.saving = false } }
async function publish(row: ReviewVersionRecord) { await ElMessageBox.confirm(`发布 V${row.version_no} 后，新提交或重新审核的报告将使用其中 ${row.rule_count} 条规则。确认发布吗？`, '发布审核版本', { type: 'warning', confirmButtonText: '确认发布' }); await publishVersion(row.id); await Promise.all([loadVersions(), loadRules(), loadSummary()]); ElMessage.success('审核版本已发布') }
async function rollback(row: ReviewVersionRecord) { await ElMessageBox.confirm(`确认回退到 V${row.version_no}"${row.name}"吗？后续审核将使用该版本的规则快照。`, '回退审核版本', { type: 'warning', confirmButtonText: '确认回退' }); await rollbackVersion(row.id); await Promise.all([loadVersions(), loadRules(), loadSummary()]); ElMessage.success('已完成版本回退') }

async function openCaseDialog() { if (!reportOptions.value.length) reportOptions.value = (await getReports({ page: 1, size: 100 })).items || []; Object.assign(caseDialog, { visible: true, saving: false, report_id: 0, name: '', expected_conclusion: '合格', expected_issue_count: 0, notes: '' }) }
async function saveCase() { if (!caseDialog.report_id || !caseDialog.name.trim()) return void ElMessage.warning('请选择报告并填写样本名称'); caseDialog.saving = true; try { await createRegressionCase({ report_id: caseDialog.report_id, name: caseDialog.name, expected_conclusion: caseDialog.expected_conclusion, expected_issue_count: caseDialog.expected_issue_count, notes: caseDialog.notes }); caseDialog.visible = false; await Promise.all([loadRegression(), loadSummary()]); ElMessage.success('已加入回归测试集') } finally { caseDialog.saving = false } }
async function toggleCase(row: RegressionCaseRecord) { await toggleRegressionCase(row.id, row.enabled); await loadSummary() }
async function runCheck() { runningRegression.value = true; try { const result = await runRegression(); await Promise.all([loadRegression(), loadSummary(), loadVersions()]); ElMessage.success(`校验完成：${result.passed}/${result.total} 通过`) } finally { runningRegression.value = false } }

onMounted(loadAll)
</script>

<style scoped>
.iteration-page { display: grid; gap: 14px; }

.leadership-hero { min-height: 182px; display: grid; grid-template-columns: 1fr 260px; align-items: stretch; gap: 32px; padding: 0; overflow: hidden; border: 0; background: linear-gradient(118deg, #102a43 0%, #183f61 58%, #1f5375 100%); color: #fff; }
.hero-main { align-self: center; padding: 30px 34px; }
.hero-status { width: fit-content; display: inline-flex; align-items: center; gap: 8px; margin-bottom: 15px; color: #d7e7f3; font-size: 12px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #4ade80; box-shadow: 0 0 0 4px rgba(74,222,128,.12); }
.hero-main h1 { margin: 0; font-size: 28px; letter-spacing: -.02em; line-height: 1.25; }
.hero-main p { max-width: 680px; margin: 12px 0 0; color: #c9dce9; font-size: 14px; line-height: 1.7; }
.hero-release { display: flex; flex-direction: column; justify-content: center; padding: 26px 30px; background: rgba(255,255,255,.075); border-left: 1px solid rgba(255,255,255,.14); }
.hero-release span, .hero-release small { color: #c9dce9; font-size: 12px; }
.hero-release strong { margin: 7px 0 2px; font-size: 36px; line-height: 1; font-variant-numeric: tabular-nums; }
.hero-release p { margin: 7px 0 13px; font-weight: 650; line-height: 1.45; }

.executive-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.metric-card { min-height: 112px; padding: 18px 20px; border: 1px solid var(--el-border-color-lighter); border-radius: 12px; background: var(--el-bg-color); }
.metric-card span, .metric-card small { display: block; color: var(--el-text-color-secondary); }
.metric-card span { font-size: 12px; }
.metric-card strong { display: block; margin: 8px 0 7px; color: #183f61; font-size: 27px; line-height: 1; font-variant-numeric: tabular-nums; }
.metric-card small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.metric-primary { border-color: color-mix(in srgb, #21618c 28%, var(--el-border-color-lighter)); background: color-mix(in srgb, #21618c 5%, var(--el-bg-color)); }
.metric-warning strong { color: var(--el-color-warning-dark-2); }

.governance-grid { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(340px, .75fr); gap: 14px; }
.release-story, .health-panel, .closed-loop, .version-journey { padding: 22px 24px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.section-heading span, .workspace-heading span { display: block; margin-bottom: 6px; color: #4f7390; font-size: 12px; font-weight: 650; }
.section-heading h2, .workspace-heading h2 { margin: 0; color: var(--el-text-color-primary); font-size: 18px; line-height: 1.35; }
.section-heading.compact { align-items: center; }
.section-heading.compact > small { max-width: 520px; text-align: right; line-height: 1.55; }
.release-description { min-height: 50px; margin: 18px 0 19px; color: var(--el-text-color-regular); line-height: 1.75; }
.release-facts { display: grid; grid-template-columns: repeat(3, 1fr); padding: 14px 0; border-block: 1px solid var(--el-border-color-lighter); }
.release-facts > div { padding-left: 16px; border-left: 2px solid #d8e5ee; }
.release-facts strong, .release-facts span { display: block; }
.release-facts strong { color: #183f61; font-size: 21px; }
.release-facts span { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
.version-components { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 15px; }
.version-components span, .material-versions span { padding: 5px 9px; border-radius: 5px; background: var(--el-fill-color-light); color: var(--el-text-color-secondary); font-size: 11px; }

.health-list { list-style: none; display: grid; gap: 0; margin: 15px 0 0; padding: 0; }
.health-list li { display: grid; grid-template-columns: 34px 1fr auto; align-items: center; gap: 11px; padding: 13px 0; border-top: 1px solid var(--el-border-color-lighter); }
.health-icon { width: 31px; height: 31px; display: grid; place-items: center; border-radius: 8px; }
.health-icon.ok { color: #18864b; background: #e9f7ef; }
.health-icon.warn { color: #a86608; background: #fff5df; }
.health-list strong, .health-list small { display: block; }
.health-list strong { margin-bottom: 3px; font-size: 13px; }
.health-list small { color: var(--el-text-color-secondary); line-height: 1.35; }
.health-list b { color: var(--el-text-color-regular); font-size: 12px; }

.loop-steps { display: grid; grid-template-columns: repeat(5, 1fr); margin-top: 20px; }
.loop-steps > div { position: relative; min-width: 0; display: grid; justify-items: start; gap: 4px; padding: 0 24px 0 0; }
.loop-steps > div:not(:last-child)::after { content: ''; position: absolute; top: 16px; left: 43px; right: 14px; height: 1px; background: #cbd9e3; }
.loop-steps i { position: relative; z-index: 1; width: 32px; height: 32px; display: grid; place-items: center; margin-bottom: 8px; border: 1px solid #9db4c5; border-radius: 50%; background: var(--el-bg-color); color: #315b78; font-style: normal; font-size: 12px; font-weight: 700; }
.loop-steps span { color: var(--el-text-color-regular); font-size: 13px; }
.loop-steps strong { color: #183f61; font-size: 22px; font-variant-numeric: tabular-nums; }
.loop-steps small { color: var(--el-text-color-secondary); }

.rule-atlas { padding: 0; overflow: hidden; border-color: #e2eaf4; background: #f9fbfe; }
.atlas-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding: 24px 25px 16px; }
.atlas-heading > div:first-child { max-width: 780px; }
.atlas-heading span, .atlas-dialog-title span { display: block; margin-bottom: 6px; color: #527394; font-size: 12px; font-weight: 650; }
.atlas-heading h2 { margin: 0; color: #17243a; font-size: 22px; letter-spacing: -.02em; }
.atlas-heading p { margin: 9px 0 0; color: #708097; line-height: 1.65; }
.atlas-actions { flex: none; display: flex; gap: 8px; }
.atlas-actions :deep(.el-button) { border-radius: 8px; }
.atlas-toolbar { display: flex; align-items: center; gap: 14px; padding: 0 25px 18px; }
.atlas-search { width: 350px; flex: none; }
.atlas-search :deep(.el-input__wrapper) { min-height: 38px; border-radius: 10px; background: #fff; box-shadow: 0 0 0 1px #dfe8f3 inset; }
.atlas-search :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px #4c8df6 inset, 0 5px 16px rgba(37,99,235,.08); }
.atlas-filters { min-width: 0; display: flex; align-items: center; gap: 6px; overflow-x: auto; padding: 2px; }
.atlas-filters button { flex: none; min-height: 34px; padding: 0 13px; border: 1px solid #e3eaf3; border-radius: 8px; background: #fff; color: #536178; font: inherit; font-size: 12px; cursor: pointer; transition: transform .18s ease, color .18s ease, border-color .18s ease, background-color .18s ease; }
.atlas-filters button:hover { color: #2563eb; border-color: #a9c7f8; }
.atlas-filters button:active { transform: translateY(1px); }
.atlas-filters button.active { border-color: #2563eb; background: #2563eb; color: #f8fbff; box-shadow: 0 5px 14px rgba(37,99,235,.18); }
.atlas-filters button:focus-visible { outline: 2px solid #8ab4f8; outline-offset: 2px; }
.atlas-layout { display: grid; grid-template-columns: 218px minmax(0, 1fr) 272px; min-height: 650px; border-top: 1px solid #e4ebf4; background: #fbfcff; }
.atlas-overview { min-width: 0; padding: 22px 17px; border-right: 1px solid #e6edf5; background: rgba(255,255,255,.88); }
.overview-title { margin-bottom: 15px; padding: 0 4px 14px; border-bottom: 1px solid #edf1f6; }
.overview-title strong, .overview-title span { display: block; }
.overview-title strong { color: #1f2c41; font-size: 14px; }
.overview-title span { margin-top: 4px; color: #8a97a9; font-size: 10px; }
.overview-stat { display: grid; grid-template-columns: 38px 1fr; align-items: center; gap: 10px; padding: 9px 4px; }
.overview-stat i { width: 36px; height: 36px; display: grid; place-items: center; border-radius: 10px; background: #eef4ff; color: #2563eb; font-size: 17px; font-style: normal; }
.overview-stat i.stat-cyan { background: #e9f9fb; color: #0d9db1; }
.overview-stat i.stat-green { background: #eaf8f3; color: #15966f; }
.overview-stat i.stat-orange { background: #fff5e8; color: #d98315; }
.overview-stat span { min-width: 0; color: #7a879a; font-size: 10px; }
.overview-stat strong { display: block; margin-top: 2px; color: #1b273b; font-size: 19px; font-variant-numeric: tabular-nums; }
.overview-total { margin-top: 8px; padding: 12px 8px; border-radius: 12px; background: #edf4ff; }
.overview-total i { background: #2563eb; color: #fff; }
.overview-total span { color: #567097; }
.overview-total strong { color: #1d5bc6; }
.overview-version { margin-top: 15px; padding: 11px 4px 0; border-top: 1px solid #edf1f6; }
.overview-version span, .overview-version b { display: block; }
.overview-version span { color: #8a97a9; font-size: 10px; }
.overview-version b { margin-top: 4px; color: #4b5a70; font-size: 10px; line-height: 1.45; overflow-wrap: anywhere; }
.atlas-canvas { position: relative; min-width: 0; overflow: hidden; background-color: #fbfcff; background-image: radial-gradient(circle at 49% 45%, rgba(85,147,237,.10), transparent 31%), radial-gradient(circle at 82% 24%, rgba(139,92,246,.045), transparent 20%), radial-gradient(circle at 28% 76%, rgba(20,184,166,.045), transparent 22%); }
.atlas-canvas::after { content: ''; pointer-events: none; position: absolute; inset: 0; box-shadow: inset 0 0 56px rgba(51,92,139,.035); }
.atlas-canvas :deep(.v-chart) { position: relative; z-index: 1; }
.atlas-legend { position: absolute; z-index: 2; left: 18px; right: 18px; bottom: 14px; display: flex; align-items: center; gap: 14px; min-height: 38px; padding: 8px 12px; border: 1px solid rgba(224,233,244,.9); border-radius: 10px; background: rgba(255,255,255,.88); color: #607086; font-size: 10px; box-shadow: 0 8px 24px rgba(54,89,132,.07); backdrop-filter: blur(8px); }
.atlas-legend span { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.atlas-legend i { width: 8px; height: 8px; border-radius: 50%; }
.atlas-legend .legend-root { background: #2563eb; }
.atlas-legend .legend-standard { background: #0ea5e9; }
.atlas-legend .legend-test { background: #f59e0b; }
.atlas-legend .legend-matrix { background: #ef6b73; }
.atlas-legend .legend-parameter { background: #8b5cf6; }
.atlas-legend small { margin-left: auto; color: #8b98aa; white-space: nowrap; }
.atlas-detail { min-width: 0; padding: 22px 19px; color: #273449; background: rgba(255,255,255,.94); border-left: 1px solid #e6edf5; }
.detail-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-bottom: 13px; border-bottom: 1px solid #edf1f6; }
.detail-heading span { color: #253247; font-size: 14px; font-weight: 700; }
.detail-heading b { padding: 4px 7px; border-radius: 6px; background: #edf4ff; color: #2563eb; font-size: 10px; font-weight: 650; }
.atlas-detail h3 { margin: 16px 0 8px; color: #17243a; font-size: 16px; line-height: 1.5; overflow-wrap: anywhere; }
.atlas-detail > p { margin: 0 0 16px; color: #7b899b; }
.atlas-detail > p b { color: #2563eb; }
.atlas-detail dl { margin: 16px 0 0; }
.atlas-detail dt { margin-top: 13px; color: #8b98aa; font-size: 10px; }
.atlas-detail dd { margin: 4px 0 0; color: #425168; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.detail-hint { margin-top: 20px; padding: 13px; border-left: 2px solid #4c8df6; border-radius: 0 8px 8px 0; background: #f3f7fd; color: #718096; font-size: 12px; line-height: 1.65; }
:deep(.atlas-dialog) { background: #f6f8fc; }
:deep(.atlas-dialog .el-dialog__header) { margin: 0; padding: 14px 22px; border-bottom: 1px solid #e1e9f2; background: #fff; }
:deep(.atlas-dialog .el-dialog__body) { padding: 0; }
:deep(.atlas-dialog .el-dialog__headerbtn .el-dialog__close) { color: #526177; }
.atlas-dialog-title { display: flex; align-items: center; justify-content: space-between; padding-right: 36px; color: #1e2b40; }
.atlas-dialog-title strong { display: block; font-size: 17px; }
.atlas-dialog-title small { color: #78869a; }
.atlas-fullscreen { background: #f7f9fc; }
.atlas-fullscreen-layout { display: grid; grid-template-columns: 230px minmax(0, 1fr) 292px; min-height: calc(100dvh - 65px); }
.atlas-overview-full { border-top: 0; }
.atlas-canvas-full { min-height: calc(100dvh - 65px); }
.atlas-detail-full { overflow: auto; max-height: calc(100dvh - 65px); }

.journey-list { margin-top: 15px; border-top: 1px solid var(--el-border-color-lighter); }
.journey-list article { display: grid; grid-template-columns: 66px 1fr 155px; gap: 16px; align-items: center; padding: 15px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.journey-list article.current { margin-inline: -12px; padding-inline: 12px; background: color-mix(in srgb, #21618c 5%, var(--el-bg-color)); }
.journey-version { color: #315b78; font-size: 17px; font-weight: 750; }
.journey-list strong { font-size: 14px; }
.journey-list p { margin: 5px 0 0; color: var(--el-text-color-secondary); line-height: 1.55; }
.journey-result { display: grid; justify-items: end; gap: 5px; }
.journey-result b { color: var(--el-text-color-regular); font-size: 12px; }
.journey-result small { color: var(--el-text-color-secondary); }

.workspace { padding-top: 16px; }
.workspace-heading { display: flex; justify-content: space-between; align-items: end; gap: 18px; margin-bottom: 9px; padding: 0 2px 15px; border-bottom: 1px solid var(--el-border-color-lighter); }
.workspace-heading p, .toolbar-note { margin: 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.materials-intro { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 8px; padding: 14px 16px; border-radius: 9px; background: var(--el-fill-color-light); }
.materials-intro p { margin: 4px 0 0; color: var(--el-text-color-secondary); }
.material-versions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.material-tabs { margin-top: 6px; }
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
.toolbar { min-height: 42px; display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.toolbar-between { justify-content: space-between; }
.active-version { display: grid; grid-template-columns: 260px 1fr; align-items: center; gap: 22px; padding: 14px 18px; margin-bottom: 12px; border: 1px solid color-mix(in srgb, var(--el-color-success) 34%, var(--el-border-color)); border-radius: 9px; background: color-mix(in srgb, var(--el-color-success) 6%, var(--el-bg-color)); }
.active-version span { display: block; color: var(--el-text-color-secondary); font-size: 12px; margin-bottom: 4px; }
.active-version strong { font-size: 16px; }
.active-version p { margin: 0; color: var(--el-text-color-regular); }
small { color: var(--el-text-color-secondary); }
.current-text { color: var(--el-color-success); font-weight: 600; }
.subsection-title { margin: 24px 0 10px; font-size: 15px; font-weight: 650; }
.result-table { padding: 10px 46px; }
.correction-detail { display: grid; gap: 16px; }
.json-compare { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.json-compare label { display: block; font-weight: 600; margin-bottom: 6px; }
.json-compare pre { min-height: 110px; max-height: 230px; overflow: auto; margin: 0; padding: 12px; border-radius: 8px; background: var(--el-fill-color-light); color: var(--el-text-color-regular); font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.rule-picker { width: 100%; display: grid; gap: 8px; max-height: 330px; overflow: auto; }
.rule-picker :deep(.el-checkbox) { height: auto; align-items: flex-start; margin: 0; padding: 10px 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; }
.rule-picker :deep(.el-checkbox__label) { display: grid; gap: 3px; white-space: normal; }
.rule-picker span { color: var(--el-text-color-secondary); font-size: 12px; }

/* 知识库浏览 */
.knowledge-browser { display: grid; grid-template-columns: 280px 1fr; gap: 16px; min-height: 480px; }
.knowledge-tree { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 12px; overflow: auto; }
.knowledge-tree .toolbar { margin-bottom: 8px; min-height: auto; }
.knowledge-tree .tree-node { display: inline-flex; align-items: center; gap: 6px; }
.knowledge-preview { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 20px; overflow: auto; background: var(--el-bg-color); }
.knowledge-preview .markdown-body { font-size: 14px; line-height: 1.7; color: var(--el-text-color-regular); }
.knowledge-preview .markdown-body :deep(h1) { font-size: 20px; border-bottom: 1px solid var(--el-border-color-lighter); padding-bottom: 8px; margin: 16px 0 12px; }
.knowledge-preview .markdown-body :deep(h2) { font-size: 17px; margin: 14px 0 10px; }
.knowledge-preview .markdown-body :deep(h3) { font-size: 15px; margin: 12px 0 8px; }
.knowledge-preview .markdown-body :deep(pre) { background: var(--el-fill-color-light); padding: 12px; border-radius: 6px; overflow: auto; font-size: 12px; }
.knowledge-preview .markdown-body :deep(code) { background: var(--el-fill-color-light); padding: 2px 6px; border-radius: 4px; font-size: 12px; }
.knowledge-preview .markdown-body :deep(ul), .knowledge-preview .markdown-body :deep(ol) { padding-left: 22px; margin: 8px 0; }
.knowledge-preview .markdown-body :deep(table) { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }
.knowledge-preview .markdown-body :deep(th), .knowledge-preview .markdown-body :deep(td) { border: 1px solid var(--el-border-color-lighter); padding: 6px 10px; }
.knowledge-preview .markdown-body :deep(th) { background: var(--el-fill-color-light); font-weight: 600; }
.knowledge-preview .markdown-body :deep(blockquote) { border-left: 3px solid var(--el-color-primary); margin: 10px 0; padding: 4px 14px; background: var(--el-fill-color-light); border-radius: 0 6px 6px 0; }

@media (max-width: 1180px) {
  .atlas-layout { grid-template-columns: 190px minmax(0, 1fr); }
  .atlas-detail { grid-column: 1 / -1; min-height: 180px; border-top: 1px solid #e6edf5; border-left: 0; }
  .atlas-fullscreen-layout { grid-template-columns: 200px minmax(0, 1fr) 260px; }
  .atlas-legend small { display: none; }
}
@media (max-width: 900px) {
  .leadership-hero { grid-template-columns: 1fr 220px; }
  .executive-metrics { grid-template-columns: 1fr 1fr; }
  .governance-grid { grid-template-columns: 1fr; }
  .atlas-toolbar { align-items: stretch; flex-direction: column; }
  .atlas-search { width: 100%; }
  .atlas-layout { grid-template-columns: 1fr; }
  .atlas-overview { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 7px; padding: 15px; border-right: 0; border-bottom: 1px solid #e6edf5; }
  .overview-title, .overview-version { grid-column: 1 / -1; }
  .overview-title { margin: 0; }
  .overview-stat { display: block; padding: 10px; border-radius: 10px; background: #f7f9fc; }
  .overview-stat i { width: 30px; height: 30px; margin-bottom: 7px; }
  .overview-total { margin: 0; background: #edf4ff; }
  .atlas-detail { grid-column: auto; }
  .atlas-fullscreen-layout { grid-template-columns: 1fr; }
  .atlas-fullscreen-layout .atlas-overview-full { display: none; }
  .atlas-detail-full { display: none; }
  .knowledge-browser { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
  .leadership-hero { grid-template-columns: 1fr; }
  .hero-main { padding: 24px; }
  .hero-release { border-top: 1px solid rgba(255,255,255,.14); border-left: 0; }
  .executive-metrics { grid-template-columns: 1fr; }
  .loop-steps { grid-template-columns: 1fr; gap: 13px; }
  .loop-steps > div { grid-template-columns: 34px 1fr auto; align-items: center; justify-items: start; padding: 0; }
  .loop-steps > div:not(:last-child)::after { top: 32px; bottom: -13px; left: 16px; right: auto; width: 1px; height: auto; }
  .loop-steps i { grid-row: 1 / span 2; margin: 0; }
  .loop-steps strong { grid-column: 3; grid-row: 1; }
  .loop-steps small { grid-column: 2 / span 2; }
  .journey-list article { grid-template-columns: 52px 1fr; }
  .journey-result { grid-column: 2; justify-items: start; }
  .section-heading.compact, .workspace-heading, .materials-intro { align-items: flex-start; flex-direction: column; }
  .atlas-heading { align-items: flex-start; flex-direction: column; }
  .atlas-actions { width: 100%; }
  .atlas-actions :deep(.el-button) { flex: 1; }
  .atlas-toolbar { padding-inline: 18px; }
  .atlas-filters { padding-bottom: 4px; }
  .atlas-overview { grid-template-columns: 1fr 1fr; }
  .overview-title, .overview-version { grid-column: 1 / -1; }
  .atlas-legend { gap: 9px; overflow-x: auto; }
  .section-heading.compact > small { text-align: left; }
  .material-versions { justify-content: flex-start; }
  .toolbar-between, .active-version { align-items: flex-start; grid-template-columns: 1fr; flex-direction: column; }
  .json-compare, .form-grid { grid-template-columns: 1fr; }
}
</style>
