一键更新功能，就是把所有的需要更新的代码都更新

增加一个更新所选按钮，就是更新选择需要更新的仓库

如果仓库变动比较大，检查一下是否删库，是否文件清零

在版本历史界面使用[Pivot](https://pyqt-fluent-widgets.readthedocs.io/zh-cn/latest/autoapi/qfluentwidgets/components/navigation/pivot/index.html#qfluentwidgets.components.navigation.pivot.Pivot制作一个远端历史和本地历史导航，远端历史窗口不再使用dialog，改用qwidget。

整体主界面使用FramelessWindow和[NavigationInterface](https://pyqt-fluent-widgets.readthedocs.io/zh-cn/latest/autoapi/qfluentwidgets/components/navigation/navigation_interface/index.html#qfluentwidgets.components.navigation.navigation_interface.NavigationInterface)做窗口和导航，代码参考https://github.com/zhiyiYo/PyQt-Fluent-Widgets/blob/master/examples/navigation/navigation2/demo.py
修复

![image-20260905184754066](C:\Users\luck\AppData\Roaming\Typora\typora-user-images\image-20260905184754066.png)错误，，本地仓库从未做过修改，一直都是pull状态。

屏蔽fluent推广信息

with contextlib.redirect_stdout(io.StringIO()):
    import qfluentwidgets.common.config 



优化算法逻辑，避免产生lock file，产生pull问题。



参考仓库存放目录的组件和布局，新增一个设置release下载目录

每次启动只清理并重新生成log日志文件，repo和软件设置的json文件并不清理。

https://github.com/AresConnor/pyqt5-concurrent优化线程处理

取消仓库列名称的tooltip显示，初始时和扫描完成后都不显示。

优化release面板进度条，扫描和下载统一为一个。使用listwidget显示，左侧是list，右侧是根据list显示的区域，并且release信息也显示，增加停止和取消以及取消后未下载完文件的清理。

克隆列表后及时部分刷新，刷新增加后的部分，不全量刷新。

添加git镜像，