import sys
import ray
import argparse
import numpy as np
from rlvr.loaders import get_gsm8k
from rlvr.actors import (
    Tokenizer,
    DeTokenizer,
    RolloutWorker,
    RolloutDispatcher,
    Replicator,
    LastIntScorer,
    ReferenceWorker,
    ReferenceDispatcher,
    GRPOLearner,
    GRPODispatcher,
)


def get_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str)
    parser.add_argument("--num-rollout-workers", type=int)
    parser.add_argument("--num-reference-workers", type=int)
    parser.add_argument("--num-grpo-learners", type=int)
    parser.add_argument("--batch-size-sync", type=int)
    parser.add_argument("--batch-size-update", type=int)
    parser.add_argument("--batch-size-backward", type=int)
    parser.add_argument("--batch-size-rollout", type=int)
    parser.add_argument("--batch-size-reference", type=int)
    parser.add_argument("--num-generations", type=int)
    parser.add_argument("--max-length", type=int)
    parser.add_argument("--temperature", type=float, default=1.0)
    args = parser.parse_args()

    assert args.batch_size_sync % args.batch_size_update == 0
    assert args.batch_size_update % args.batch_size_backward == 0
    assert args.batch_size_backward % args.batch_size_rollout == 0
    repl_batch_size_backward = args.batch_size_backward * args.num_generations
    assert repl_batch_size_backward % args.batch_size_reference == 0
    assert repl_batch_size_backward % args.num_grpo_learners == 0
    args.batch_size_learner = repl_batch_size_backward // args.num_grpo_learners

    return args


def main(argv):
    args = get_args(argv)
    ray.init()

    # Initialize workers
    rollout_workers = [
        RolloutWorker.remote(args.model) for _ in range(args.num_rollout_workers)
    ]
    ref_workers = [
        ReferenceWorker.remote(args.model) for _ in range(args.num_reference_workers)
    ]
    grpo_workers = [
        GRPOLearner.remote(args.model) for _ in range(args.num_grpo_learners)
    ]

    # Initialize torch.distributed group
    global_dist_group = grpo_workers + rollout_workers
    host, port = ray.get(global_dist_group[0].get_addr.remote())
    for rank, worker in enumerate(global_dist_group):
        worker.init_process_group.remote(host, port, len(global_dist_group), rank)

    weight_share_group = [grpo_workers[0]] + rollout_workers
    weight_share_ranks = ray.get([w.get_rank.remote() for w in weight_share_group])
    for rank, worker in enumerate(weight_share_group):
        worker.new_group.remote(weight_share_ranks, group_name="weight-share")

    ddp_ranks = ray.get([w.get_rank.remote() for w in grpo_workers])
    for rank, worker in enumerate(grpo_workers):
        worker.new_group.remote(ddp_ranks, group_name="ddp")
        worker.distribute.remote(group_name="ddp")

    # Initialize actors
    tokenizer = Tokenizer.remote(args.model)
    rollout_dispatcher = RolloutDispatcher.remote(rollout_workers)
    detokenizer = DeTokenizer.remote(args.model)
    replicator = Replicator.remote()
    scorer = LastIntScorer.remote()
    ref_dispatcher = ReferenceDispatcher.remote(ref_workers)
    grpo_dispatcher = GRPODispatcher.remote(grpo_workers)

    # Main loop
    dataloader = get_gsm8k()
    for batch in dataloader.iter_batches(batch_size=args.batch_size_sync):
        loss_refs = []
        for bgn_update in range(0, args.batch_size_sync, args.batch_size_update):
            for bgn_backward in range(
                0, args.batch_size_update, args.batch_size_backward
            ):
                s = slice(
                    bgn_update + bgn_backward,
                    bgn_update + bgn_backward + args.batch_size_backward,
                )
                questions = batch["question"][s]
                answers = batch["answer"][s]
                input_ids_ref, attention_mask_ref = tokenizer.process.remote(
                    texts=questions,
                    apply_chat_template=False,
                )
                (
                    input_outputs_ref,
                    input_output_mask_ref,
                    output_mask_ref,
                    output_log_probs_ref,
                ) = rollout_dispatcher.process.remote(
                    input_ids=input_ids_ref,
                    attention_mask=attention_mask_ref,
                    batch_size=args.batch_size_rollout,
                    max_length=args.max_length,
                    do_sample=True,
                    temperature=args.temperature,
                    num_return_sequences=args.num_generations,
                )
                output_texts_ref = detokenizer.process.remote(
                    tokens=input_outputs_ref,
                    attention_mask=output_mask_ref,
                )
                repl_answers_ref = replicator.process.remote(
                    arr=answers,
                    num_replica=args.num_generations,
                )
                scores_ref = scorer.process.remote(
                    responses=output_texts_ref,
                    answers=repl_answers_ref,
                )
                ref_log_probs_ref = ref_dispatcher.process.remote(
                    input_output_ids=input_outputs_ref,
                    input_output_mask=input_output_mask_ref,
                    batch_size=args.batch_size_reference,
                )
                loss_ref = grpo_dispatcher.process.remote(
                    num_generations=args.num_generations,
                    input_output_ids=input_outputs_ref,
                    input_output_mask=input_output_mask_ref,
                    output_log_probs=output_log_probs_ref,
                    output_mask=output_mask_ref,
                    ref_log_probs=ref_log_probs_ref,
                    scores=scores_ref,
                    batch_size=args.batch_size_learner,
                )
                loss_refs.append(loss_ref)
            grpo_dispatcher.update.remote(loss_ref)
        losses = ray.get(loss_refs)
        print("loss =", np.concatenate(losses).mean())

        src_rank = ray.get(weight_share_group[0].get_rank.remote())
        for worker in weight_share_group:
            worker.sync.remote(src_rank, "weight-share")

    ray.shutdown()


if __name__ == "__main__":
    main(sys.argv)
