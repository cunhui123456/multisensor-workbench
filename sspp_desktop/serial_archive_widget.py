"""串口历史图表与可撤销标定点排除；原始文件始终只读。"""
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg
from .plots import make_plot,COLORS
from .qt_cleanup import close_plot_widgets


class SerialArchiveWidget(QtWidgets.QWidget):
    calibration_delete_requested = QtCore.Signal(object)
    calibration_restore_requested = QtCore.Signal(object)

    def __init__(self):
        super().__init__();layout=QtWidgets.QVBoxLayout(self)
        self._details_key = None
        self._calibration_points = {}
        self._calibration_editable = False
        self._edit_busy = False
        self.summary=QtWidgets.QLabel('选择温压／温度标定档案后查看；此处为历史数据。')
        self.summary.setWordWrap(True);layout.addWidget(self.summary)
        self.voltage=make_plot('历史实收电压','主机接收经过时间（s）','电压（V）')
        self.adc=make_plot('历史ADC','主机接收经过时间（s）','ADC码值')
        self.voltage.addLegend();self.adc.addLegend();self.adc.setXLink(self.voltage)
        row=QtWidgets.QHBoxLayout();row.addWidget(self.voltage);row.addWidget(self.adc);layout.addLayout(row,1)
        self.table=QtWidgets.QTableWidget(0,6)
        self.table.setHorizontalHeaderLabels(['原始点 ID','时长 s','参考均温 ℃','S6均值±SD V','S7均值±SD V','状态 / 排除标记'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._update_calibration_actions)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(220);layout.addWidget(self.table)
        actions=QtWidgets.QHBoxLayout()
        self.calibration_delete_button=QtWidgets.QPushButton('删除错误标定点…')
        self.calibration_restore_button=QtWidgets.QPushButton('恢复已删除点…')
        self.calibration_delete_button.clicked.connect(self._request_calibration_delete)
        self.calibration_restore_button.clicked.connect(self._request_calibration_restore)
        actions.addWidget(self.calibration_delete_button);actions.addWidget(self.calibration_restore_button)
        actions.addStretch();layout.addLayout(actions)
        self.calibration_edit_status=QtWidgets.QLabel()
        self.calibration_edit_status.setWordWrap(True);layout.addWidget(self.calibration_edit_status)
        self._update_calibration_actions()
        self.lines=[]

    def display(self,details):
        self._details_key = details.get('key')
        self._edit_busy = False
        for plot in (self.voltage,self.adc):
            plot.clear()
            if plot.plotItem.legend:plot.plotItem.legend.clear()
        self.lines=[]
        curve=details.get('curve',{});rows=curve.get('points',[]);metadata=details.get('metadata',{})
        self.summary.setText(f"历史只读档案 · {details['key']}\n样品：{metadata.get('sample_name',metadata.get('label',''))} · 状态：{metadata.get('status','')} · 原始样本 {curve.get('stored_rows',0)} 行；当前显示 {len(rows)} 行")
        for i,ch in enumerate(sorted({r['channel'] for r in rows})):
            selected=[r for r in rows if r['channel']==ch]
            x=[];voltage=[];adc=[];previous=None
            for r in selected:
                if previous and (r['time_segment']!=previous['time_segment'] or r['host_elapsed_s']-previous['host_elapsed_s']>3):
                    x.append(r['host_elapsed_s']);voltage.append(float('nan'));adc.append(float('nan'))
                x.append(r['host_elapsed_s']);voltage.append(r['voltage_v']);adc.append(r['adc'] if r['adc'] is not None else float('nan'));previous=r
            for plot,values in ((self.voltage,voltage),(self.adc,adc)):
                self.lines.append(plot.plot(x,values,pen=pg.mkPen(COLORS[i%len(COLORS)],width=1.5),name=ch,connect='finite'))
        self._calibration_editable = (
            details.get('context',{}).get('experiment') == 'temperature_calibration'
            and metadata.get('status') == 'complete'
            and all(metadata.get(field) is True for field in
                    ('serial_closed','workers_stopped','data_files_closed')))
        points=details.get('calibration_points',[])
        self._calibration_points = {p['id']: p for p in points
                                    if isinstance(p.get('id'),int) and not isinstance(p['id'],bool)}
        self.table.blockSignals(True)
        self.table.clearSelection();self.table.setCurrentItem(None)
        self.table.setRowCount(len(points))
        for i,p in enumerate(points):
            voltages=[]
            for ch in ('S6','S7'):
                v=p.get('channels',{}).get(ch)
                voltages.append('—' if not v else f"{v['mean_v']:.5f} ± {v['sample_sd_v']:.5f}" if v.get('sample_sd_v') is not None else f"{v['mean_v']:.5f}（单样本）")
            state_parts = ['已删除（可恢复）' if p.get('deleted') is True else
                           '已保存' if p.get('status') == 'saved' else p.get('status','')]
            state_parts.extend(p.get('quality_flags',[]))
            if p.get('requires_manual_review'):
                state_parts.append('需人工复核')
            deletion=p.get('deletion') or {}
            if p.get('deleted') is True and deletion.get('reason'):
                state_parts.append(str(deletion['reason']))
            state='；'.join(part for part in state_parts if part)
            for j,v in enumerate([p.get('id'),p.get('duration_s'),p.get('reference_mean_c'),*voltages,state]):
                item=QtWidgets.QTableWidgetItem('—' if v is None else f'{v:.3f}' if isinstance(v,float) else str(v))
                item.setData(QtCore.Qt.ItemDataRole.UserRole,p.get('id'))
                if p.get('deleted') is True:
                    item.setForeground(QtGui.QColor('#757575'))
                    item.setToolTip('已从标定展示与组合分析排除；原始记录保留。\n'
                                    +str(deletion.get('reason',''))+'\n'+str(deletion.get('deleted_utc','')))
                self.table.setItem(i,j,item)
        self.table.blockSignals(False)
        self._update_calibration_actions()
        for plot in (self.voltage,self.adc):plot.enableAutoRange()

    def _selected_point_ids(self):
        return [self.table.item(index.row(),0).data(QtCore.Qt.ItemDataRole.UserRole)
                for index in sorted(self.table.selectionModel().selectedRows(),key=lambda item:item.row())
                if self.table.item(index.row(),0) is not None]

    def _eligible_points(self,point_ids,deleted):
        return (self._calibration_editable and bool(point_ids)
                and all(isinstance(ident,int) and not isinstance(ident,bool)
                        and ident in self._calibration_points
                        and self._calibration_points[ident].get('status') == 'saved'
                        and (self._calibration_points[ident].get('deleted') is True) == deleted
                        for ident in point_ids))

    def _update_calibration_actions(self):
        point_ids=self._selected_point_ids()
        can_delete=self._eligible_points(point_ids,False)
        can_restore=self._eligible_points(point_ids,True)
        self.table.setEnabled(not self._edit_busy)
        self.calibration_delete_button.setEnabled(not self._edit_busy and can_delete)
        self.calibration_restore_button.setEnabled(not self._edit_busy and can_restore)
        explanation='删除可恢复；原始文件和原始电压曲线保留，变更记录在独立撤销账本中。'
        if self._edit_busy:
            state='正在保存标定点变更…'
        elif not self._calibration_editable:
            state='仅完整封存的温度标定记录可修改标定点。'
        elif point_ids and not (can_delete or can_restore):
            state='请分别选择已保存的有效点或已删除点；未保存点不可操作。'
        elif point_ids:
            state=f'已选择 {len(point_ids)} 个'+('已删除点。' if can_restore else '有效标定点。')
        else:
            state='可按住 Command / Shift 多选标定点。'
        self.calibration_edit_status.setText(state+'\n'+explanation)

    def set_edit_busy(self,busy):
        self._edit_busy=bool(busy)
        self._update_calibration_actions()

    def _request_calibration_delete(self):
        point_ids=self._selected_point_ids()
        if self._edit_busy or not self._eligible_points(point_ids,False):
            return
        key=self._details_key
        reason,accepted=QtWidgets.QInputDialog.getText(
            self,'删除错误标定点',f'删除所选 {len(point_ids)} 个标定点的理由（可恢复）：',
            QtWidgets.QLineEdit.EchoMode.Normal,'标注错误')
        if not accepted:
            return
        reason=reason.strip()
        if not reason:
            self.calibration_edit_status.setText('请填写删除理由；标定点尚未修改。')
            return
        # The modal input dialog runs an event loop: a completed background read
        # may have replaced the displayed record while the user entered a reason.
        if self._edit_busy or key != self._details_key or not self._eligible_points(point_ids,False):
            self.calibration_edit_status.setText('档案已更新，请重新选择需要删除的标定点。')
            return
        self.set_edit_busy(True)
        self.calibration_delete_requested.emit({'key':key,'point_ids':point_ids,'reason':reason})

    def _request_calibration_restore(self):
        point_ids=self._selected_point_ids()
        if self._edit_busy or not self._eligible_points(point_ids,True):
            return
        self.set_edit_busy(True)
        self.calibration_restore_requested.emit({
            'key':self._details_key,'point_ids':point_ids,'reason':'恢复误删标定点'})

    def closeEvent(self,event):
        self.adc.setXLink(None);close_plot_widgets(self);super().closeEvent(event)
