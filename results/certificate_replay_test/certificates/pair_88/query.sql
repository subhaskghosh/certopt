SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS PP
LEFT JOIN ADDRESS AS AA
  ON aa.personid = pp.personid