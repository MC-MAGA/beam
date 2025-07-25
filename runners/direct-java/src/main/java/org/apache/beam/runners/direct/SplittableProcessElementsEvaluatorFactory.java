/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.beam.runners.direct;

import static org.apache.beam.vendor.guava.v32_1_2_jre.com.google.common.base.Preconditions.checkArgument;

import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import org.apache.beam.runners.core.DoFnRunners;
import org.apache.beam.runners.core.KeyedWorkItem;
import org.apache.beam.runners.core.OutputAndTimeBoundedSplittableProcessElementInvoker;
import org.apache.beam.runners.core.ProcessFnRunner;
import org.apache.beam.runners.core.SideInputReader;
import org.apache.beam.runners.core.SplittableParDoViaKeyedWorkItems.ProcessElements;
import org.apache.beam.runners.core.SplittableParDoViaKeyedWorkItems.ProcessFn;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.runners.AppliedPTransform;
import org.apache.beam.sdk.transforms.DoFnSchemaInformation;
import org.apache.beam.sdk.values.KV;
import org.apache.beam.sdk.values.PCollection;
import org.apache.beam.sdk.values.PCollectionTuple;
import org.apache.beam.vendor.guava.v32_1_2_jre.com.google.common.cache.CacheLoader;
import org.apache.beam.vendor.guava.v32_1_2_jre.com.google.common.util.concurrent.MoreExecutors;
import org.apache.beam.vendor.guava.v32_1_2_jre.com.google.common.util.concurrent.ThreadFactoryBuilder;
import org.joda.time.Duration;

@SuppressWarnings({
  "nullness" // TODO(https://github.com/apache/beam/issues/20497)
})
class SplittableProcessElementsEvaluatorFactory<
        InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT>
    implements TransformEvaluatorFactory {
  private final ParDoEvaluatorFactory<KeyedWorkItem<byte[], KV<InputT, RestrictionT>>, OutputT>
      delegateFactory;
  private final ScheduledExecutorService ses;
  private final EvaluationContext evaluationContext;
  private final PipelineOptions options;

  SplittableProcessElementsEvaluatorFactory(
      EvaluationContext evaluationContext, PipelineOptions options) {
    this.evaluationContext = evaluationContext;
    this.options = options;
    this.delegateFactory =
        new ParDoEvaluatorFactory<>(
            evaluationContext,
            SplittableProcessElementsEvaluatorFactory
                .<InputT, OutputT, RestrictionT>processFnRunnerFactory(),
            new CacheLoader<AppliedPTransform<?, ?, ?>, DoFnLifecycleManager>() {
              @Override
              public DoFnLifecycleManager load(final AppliedPTransform<?, ?, ?> application) {
                checkArgument(
                    ProcessElements.class.isInstance(application.getTransform()),
                    "No know extraction of the fn from " + application);
                final ProcessElements<
                        InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT>
                    transform =
                        (ProcessElements<
                                InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT>)
                            application.getTransform();
                return DoFnLifecycleManager.of(transform.newProcessFn(transform.getFn()), options);
              }
            },
            options);
    this.ses =
        Executors.newSingleThreadScheduledExecutor(
            new ThreadFactoryBuilder()
                .setThreadFactory(MoreExecutors.platformThreadFactory())
                .setNameFormat(
                    "direct-splittable-process-element-checkpoint-executor_" + hashCode())
                .build());
  }

  @Override
  public <T> TransformEvaluator<T> forApplication(
      AppliedPTransform<?, ?, ?> application, CommittedBundle<?> inputBundle) throws Exception {
    @SuppressWarnings({"unchecked", "rawtypes"})
    TransformEvaluator<T> evaluator =
        (TransformEvaluator<T>)
            createEvaluator((AppliedPTransform) application, (CommittedBundle) inputBundle);
    return evaluator;
  }

  @Override
  public void cleanup() throws Exception {
    ses.shutdownNow(); // stop before cleaning
    delegateFactory.cleanup();
  }

  @SuppressWarnings({"unchecked", "rawtypes"})
  private TransformEvaluator<KeyedWorkItem<byte[], KV<InputT, RestrictionT>>> createEvaluator(
      AppliedPTransform<
              PCollection<KeyedWorkItem<byte[], KV<InputT, RestrictionT>>>,
              PCollectionTuple,
              ProcessElements<InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT>>
          application,
      CommittedBundle<InputT> inputBundle)
      throws Exception {
    final ProcessElements<InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT>
        transform = application.getTransform();

    final DoFnLifecycleManagerRemovingTransformEvaluator<
            KeyedWorkItem<byte[], KV<InputT, RestrictionT>>>
        evaluator =
            delegateFactory.createEvaluator(
                (AppliedPTransform) application,
                (PCollection<KeyedWorkItem<byte[], KV<InputT, RestrictionT>>>)
                    inputBundle.getPCollection(),
                inputBundle.getKey(),
                application.getTransform().getSideInputs(),
                application.getTransform().getMainOutputTag(),
                application.getTransform().getAdditionalOutputTags().getAll(),
                DoFnSchemaInformation.create(),
                application.getTransform().getSideInputMapping());
    final ParDoEvaluator<KeyedWorkItem<byte[], KV<InputT, RestrictionT>>> pde =
        evaluator.getParDoEvaluator();
    final ProcessFn<InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT> processFn =
        (ProcessFn<InputT, OutputT, RestrictionT, PositionT, WatermarkEstimatorStateT>)
            ProcessFnRunner.class.cast(pde.getFnRunner()).getFn();

    final DirectExecutionContext.DirectStepContext stepContext = pde.getStepContext();
    processFn.setStateInternalsFactory(key -> stepContext.stateInternals());
    processFn.setTimerInternalsFactory(key -> stepContext.timerInternals());

    SideInputReader sideInputReader =
        evaluationContext.createSideInputReader(transform.getSideInputs());
    processFn.setSideInputReader(sideInputReader);
    processFn.setProcessElementInvoker(
        new OutputAndTimeBoundedSplittableProcessElementInvoker<>(
            transform.getFn(),
            options,
            pde.getOutputManager(),
            transform.getMainOutputTag(),
            sideInputReader,
            ses,
            // Setting small values here to stimulate frequent checkpointing and better exercise
            // splittable DoFn's in that respect.
            100,
            Duration.standardSeconds(1),
            stepContext::bundleFinalizer));

    return evaluator;
  }

  private static <InputT, OutputT, RestrictionT>
      ParDoEvaluator.DoFnRunnerFactory<KeyedWorkItem<byte[], KV<InputT, RestrictionT>>, OutputT>
          processFnRunnerFactory() {
    return (options,
        fn,
        sideInputs,
        sideInputReader,
        outputManager,
        mainOutputTag,
        additionalOutputTags,
        stepContext,
        inputCoder,
        outputCoders,
        windowingStrategy,
        doFnSchemaInformation,
        sideInputMapping) -> {
      ProcessFn<InputT, OutputT, RestrictionT, ?, ?> processFn = (ProcessFn) fn;
      return DoFnRunners.newProcessFnRunner(
          processFn,
          options,
          sideInputs,
          sideInputReader,
          outputManager,
          mainOutputTag,
          additionalOutputTags,
          stepContext,
          inputCoder,
          outputCoders,
          windowingStrategy,
          doFnSchemaInformation,
          sideInputMapping);
    };
  }
}
