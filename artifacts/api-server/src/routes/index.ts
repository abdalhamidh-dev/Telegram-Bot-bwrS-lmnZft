import { Router, type IRouter } from "express";
import healthRouter from "./health";
import botRouter from "./bot";
import dashboardRouter from "./dashboard";

const router: IRouter = Router();

router.use(healthRouter);
router.use(botRouter);
router.use(dashboardRouter);

export default router;
