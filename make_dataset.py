"""Config-driven gVXR dataset CLI. Usage: python make_dataset.py config.yaml"""
import argparse, json, os, yaml
import ct_core as core
import ct_variations as var


def main(config_path):
    cfgd=yaml.safe_load(open(config_path))
    base=cfgd.get("scan",{})
    cfg_dir=os.path.dirname(os.path.abspath(config_path))
    obj_cfg=[]
    for o in cfgd.get("objects",[]):
        o=dict(o); stl=o.get("stl_path","")
        if stl and not os.path.isabs(stl): o["stl_path"]=os.path.join(cfg_dir,stl)
        obj_cfg.append(core.ObjectCfg(**o))
    cfg=core.ScanCfg(objects=obj_cfg, **base)
    fx=var.EffectsCfg(**cfgd.get("effects",{}))
    sampling=cfgd.get("sampling",{})
    mode=sampling.get("mode","ofat")
    if mode == "random":
        params={"n":int(sampling["n"]),"ranges":sampling["ranges"],"seed":int(sampling.get("seed",0))}
        plans=var.plan_runs(cfg,fx,params,"random",baseline=bool(sampling.get("baseline",False)))
        for plan in plans:
            # Pass each sampled setting as a single-value OFAT run.
            sampled_cfg=cfg; sampled_fx=fx
            for key,val in plan["changed"].items():
                if var.is_fx_key(key): sampled_fx=var.replace(sampled_fx,**{key[3:]:val})
                else: sampled_cfg=var.set_param(sampled_cfg,key,val)
            out_root=cfgd.get("output","dataset")
            if not os.path.isabs(out_root): out_root=os.path.join(cfg_dir,out_root)
            folder=os.path.join(out_root,plan["name"])
            var.generate(sampled_cfg,sampled_fx,[],folder,mode="ofat",baseline=True,resume=True,progress=lambda p,m:print(f"{p:.0%} {m}"))
    else:
        params=var.parse_sweep(sampling.get("params",[]),cfg,fx)
        out=cfgd.get("output","dataset")
        if not os.path.isabs(out): out=os.path.join(cfg_dir,out)
        os.makedirs(out,exist_ok=True)
        # Keep completed samples so reruns resume after a failure.
        var.generate(cfg,fx,params,out,mode=mode,baseline=bool(sampling.get("baseline",True)),
                     clean_copy=bool(sampling.get("clean_copy",True)),resume=True,
                     progress=lambda p,m:print(f"{p:.0%} {m}"))

if __name__ == "__main__":
    p=argparse.ArgumentParser(); p.add_argument("config",help="YAML config file")
    main(p.parse_args().config)
