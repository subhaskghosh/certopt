SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS T1
LEFT JOIN ADDRESS AS T2
  ON t1.personid = t2.personid